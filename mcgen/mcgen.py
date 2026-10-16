"""mcgen —— Minecraft 模组生成器

把一段自然语言需求变成能编译通过的 Fabric 模组工程。

用法：
    python3 mcgen.py new --req "加一把会爆炸的钻石剑" -o ./out
    python3 mcgen.py new --spec spec.json -o ./out
    python3 mcgen.py new --demo -o ./out             # 不调 LLM，跑内置样例
    python3 mcgen.py check --spec spec.json          # 只校验 spec

选项：
    --spec FILE     从文件读 spec（JSON）
    --req TEXT      自然语言需求（调 LLM 转成 spec）
    --modid NAME    强制指定模组 ID，覆盖推导结果
    --mirror NAME   gradle 分发包的下载源：cn（默认，腾讯镜像）/ official
    -o DIR          输出目录（必填）

LLM 选项（都不填时，从环境变量或 ~/.claude/settings.json 读现成配置）：
    --api-key KEY  --base-url URL  --model NAME  --format anthropic|openai
"""

from __future__ import annotations

import json
import os
import sys

import mcgen_build
import mcgen_llm
import mcgen_render
import mcgen_spec

DEMO_SPEC = {
    'modid': 'testmod',
    'name': 'Test Mod',
    'package': 'com.example.testmod',
    'description': 'mcgen 的最小闭环验证模组',
    'authors': ['zzy'],
    'content': [],
}


# ---------- 命令行 ----------

def arg(flag: str, default=None):
    """取 --flag 后面那个值。没有就用默认值。"""
    if flag in sys.argv:
        idx = sys.argv.index(flag) + 1
        if idx < len(sys.argv):
            return sys.argv[idx]
        print('%s 后面缺少取值' % flag)
        sys.exit(1)
    return default


def has(flag: str) -> bool:
    return flag in sys.argv


# ---------- 子命令 ----------

def load_spec() -> dict:
    """按命令行参数把 spec 搞出来。"""
    spec_path = arg('--spec')
    if spec_path:
        if not os.path.exists(spec_path):
            print('找不到 spec 文件：%s' % spec_path)
            sys.exit(1)
        try:
            with open(spec_path, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print('spec 读取失败：%s' % exc)
            sys.exit(1)

    if has('--demo'):
        return dict(DEMO_SPEC)

    req = arg('--req')
    if req:
        return spec_from_req(req)

    print(__doc__)
    sys.exit(1)


def spec_from_req(req: str) -> dict:
    """调 LLM 把中文需求翻成 spec。"""
    try:
        cfg = mcgen_llm.resolve_config(
            cli_key=arg('--api-key'),
            cli_base=arg('--base-url'),
            cli_model=arg('--model'),
            cli_format=arg('--format'),
        )
    except mcgen_llm.LLMError as exc:
        print('LLM 配置有问题：%s' % exc)
        sys.exit(1)

    print('[LLM] 格式 %s  端点 %s  模型 %s'
          % (cfg['format'], cfg['base_url'], cfg['model']))
    print('      正在把需求翻成 spec……')
    try:
        return mcgen_spec.spec_from_request(
            lambda system, user: mcgen_llm.llm_chat(cfg, system, user), req)
    except (mcgen_llm.LLMError, ValueError) as exc:
        print('需求转 spec 失败：%s' % exc)
        sys.exit(1)


def prepare_spec() -> dict:
    """拿到 spec 并做最终归一化，含 --modid 覆盖。"""
    spec = mcgen_spec.apply_defaults(load_spec())
    forced = arg('--modid')
    if forced:
        spec['modid'] = mcgen_spec.normalize_modid(forced)
        # package 若还是从旧 modid 推出来的默认值，跟着改；
        # 用户显式写过的 package 不动
        if (spec.get('package') or '').startswith('com.example.'):
            spec['package'] = 'com.example.' + spec['modid']
    return spec


def cmd_check():
    spec = prepare_spec()
    problems = mcgen_spec.validate(spec)
    if problems:
        print('spec 有 %d 个问题：' % len(problems))
        for p in problems:
            print('  - ' + p)
        sys.exit(1)
    print('spec 校验通过：%s' % spec['modid'])
    for t in mcgen_spec.unsupported(spec):
        print('  注意：类型 %r 尚未实现，会被跳过' % t)


def cmd_new():
    out_dir = arg('-o')
    if not out_dir:
        print('缺少 -o 输出目录')
        sys.exit(1)

    spec = prepare_spec()
    problems = mcgen_spec.validate(spec)
    if problems:
        print('spec 校验不通过：')
        for p in problems:
            print('  - ' + p)
        sys.exit(1)

    if os.path.exists(out_dir) and os.listdir(out_dir):
        print('输出目录非空，拒绝覆盖：%s' % out_dir)
        sys.exit(1)

    mirror = arg('--mirror', 'cn')
    if mirror not in mcgen_render.GRADLE_DIST_MIRRORS:
        print('未知镜像：%s（可选：%s）'
              % (mirror, ', '.join(mcgen_render.GRADLE_DIST_MIRRORS)))
        sys.exit(1)

    print('[1/3] 渲染工程骨架  %s（镜像 %s）' % (spec['modid'], mirror))
    written = mcgen_render.render_project(spec, out_dir, mirror)
    print('      生成 %d 个文件' % len(written))

    skipped = mcgen_spec.unsupported(spec)
    if skipped:
        print('      跳过未实现的类型：%s' % ', '.join(sorted(set(skipped))))

    print('[2/3] gradlew build')
    print('      首次要下载 Gradle、Minecraft 与依赖，跨网络可能 20 分钟以上。')
    print('      gradle 不打印下载进度，下面每 30 秒给一次心跳，静默是正常的。')
    java_home = mcgen_build.find_java_home()
    if not java_home:
        print('      找不到 JDK 25。装一个：brew install openjdk@25')
        print('      工程已生成在 %s，装好 JDK 后可自行 cd 进去跑 ./gradlew build' % out_dir)
        sys.exit(1)
    print('      JAVA_HOME = %s' % java_home)

    ok, output = mcgen_build.run_build(out_dir, first=True)
    if not ok:
        print('[3/3] 构建失败')
        print('--- 编译错误 ---')
        print(mcgen_build.extract_errors(output))
        print('--- 完整输出见 %s ---' % out_dir)
        sys.exit(1)

    print('[3/3] 构建成功')
    libs = os.path.join(out_dir, 'build', 'libs')
    if os.path.isdir(libs):
        for name in sorted(os.listdir(libs)):
            if name.endswith('.jar'):
                print('      → %s' % os.path.join(libs, name))


# ---------- 入口 ----------

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == 'new':
        cmd_new()
    elif cmd == 'check':
        cmd_check()
    else:
        print('未知子命令：%s' % cmd)
        print(__doc__)
        sys.exit(1)


if __name__ == '__main__':
    main()
