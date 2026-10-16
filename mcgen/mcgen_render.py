"""把 spec 渲染成一个完整的 Fabric 模组工程。

模板分两层：
  templates/fabric-26.3/static/  —— 原样拷贝，不做任何替换（wrapper、gradlew、LICENSE）
  templates/fabric-26.3/tpl/     —— 带 {{占位符}} 的文本模板

占位符用 {{...}} 而不是 ${...}，避免和 Groovy 的字符串插值、以及
fabric.mod.json 里交给 gradle expand 的 ${version} 撞车。
"""

from __future__ import annotations

import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_ROOT = os.path.join(HERE, 'templates', 'fabric-26.3')

# 已实测于 FabricMC/fabric-example-mod 的 26.3 分支，不要凭记忆改
VERSIONS = {
    'mc_version': '26.3',
    'loader_version': '0.19.5',
    'loom_version': '1.18-SNAPSHOT',
    'fabric_api_version': '0.162.0+26.3',
    'gradle_version': '9.7.1',
}

# Gradle 分发源。实测（2026-10，本机网络）：
#   官方 services.gradle.org 约 60KB/s —— 130MB 要 36 分钟，会被误当成卡死
#   腾讯镜像约 828KB/s —— 约 2.7 分钟
# 所以默认给 cn，别让首次构建耗掉半小时。
GRADLE_DIST_MIRRORS = {
    'official': 'https://services.gradle.org/distributions/gradle-{v}-bin.zip',
    'cn': 'https://mirrors.cloud.tencent.com/gradle/gradle-{v}-bin.zip',
}

# 依赖仓库镜像。
#
# 实测结论：**默认不加**。曾经给 cn 配过 aliyun 的 maven 镜像，结果构建卡死——
# gradle 把连接堆到 aliyun（39.91.185.84）上反复 CLOSE_WAIT，十几分钟零文件写入。
# 原因是构建要拉的大头是 Fabric maven 与 Mojang，而 aliyun 只镜像 Maven Central，
# 镜像命中不了的请求全变成重试噪音。
# 所以只镜像 gradle 分发包（那才是真正的瓶颈，14 倍提速），maven 交给原源。
# 留这个开关是为了将来遇到确实需要镜像的网络环境时不改结构。
MAVEN_MIRRORS = {
    'official': [],
    'cn': [],
}


def dist_url(mirror: str) -> str:
    """算出 gradle 分发包的下载地址。properties 文件里冒号必须转义。"""
    tmpl = GRADLE_DIST_MIRRORS.get(mirror) or GRADLE_DIST_MIRRORS['official']
    return tmpl.format(v=VERSIONS['gradle_version']).replace(':', '\\:')


# ---------- 占位符替换 ----------

def fill(text: str, values: dict) -> str:
    """把 {{key}} 换成 values[key]。没有对应键的占位符原样留着，方便发现遗漏。"""
    for key, val in values.items():
        text = text.replace('{{' + key + '}}', str(val))
    return text


def read_tpl(rel: str) -> str:
    with open(os.path.join(TEMPLATE_ROOT, 'tpl', rel), encoding='utf-8') as f:
        return f.read()


def write_out(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


# ---------- 派生字段 ----------

def authors_json(authors: list) -> str:
    """渲染成 fabric.mod.json 里 authors 数组的内容，每项一行、缩进到数组内。"""
    if not authors:
        authors = ['Anonymous']
    return '\n'.join('\t\t' + json.dumps(a, ensure_ascii=False) for a in authors)


def mod_class_name(modid: str) -> str:
    """modid 转成合法 Java 类名：boom_sword -> BoomSword。"""
    parts = [p for p in modid.replace('-', '_').split('_') if p]
    return ''.join(p[:1].upper() + p[1:] for p in parts) or 'Mod'


def maven_repo_block(mirror: str, indent: str) -> str:
    """把镜像仓库渲染成 groovy 的 maven { } 片段。official 镜像下返回空串。"""
    out = []
    for name, url in MAVEN_MIRRORS.get(mirror) or []:
        out.append('%smaven { name = %r; url = %r }' % (indent, name, url))
    return '\n'.join(out)


def base_values(spec: dict, mirror: str = 'cn') -> dict:
    """所有模板都要用的公共占位符。"""
    modid = spec['modid']
    package = spec['package']
    values = {
        'modid': modid,
        'mod_name': spec.get('name') or modid,
        'description': spec.get('description') or '',
        'package': package,
        'package_path': package.replace('.', '/'),
        'mod_class': mod_class_name(modid),
        'authors_json': authors_json(spec.get('authors') or []),
        'version': spec.get('version') or '1.0.0',
        'group': spec.get('group') or package,
        'distribution_url': dist_url(mirror),
        'maven_repos': maven_repo_block(mirror, '\t'),
        'plugin_maven_repos': maven_repo_block(mirror, '\t\t'),
    }
    values.update(VERSIONS)
    return values


# ---------- 静态文件 ----------

def copy_static(out_dir: str) -> list:
    """原样拷贝 static/ 下的所有内容。gradlew 必须保住可执行位。"""
    src_root = os.path.join(TEMPLATE_ROOT, 'static')
    written = []
    for cur, _dirs, files in os.walk(src_root):
        for name in files:
            src = os.path.join(cur, name)
            rel = os.path.relpath(src, src_root)
            dst = os.path.join(out_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            written.append(rel)
    # copyfile 不带权限，gradlew 的可执行位要显式补回来，否则 ./gradlew 跑不了
    gradlew = os.path.join(out_dir, 'gradlew')
    if os.path.exists(gradlew):
        os.chmod(gradlew, 0o755)
    return written


# ---------- 渲染 ----------

def register_calls(spec: dict) -> str:
    """生成 onInitialize() 里的注册调用块。

    返回值契约：每行以两个 tab 缩进，且整体以换行结尾；
    返回空串时调用方那行会被压掉，不留空行（见 ModClass.java 模板）。

    P1 阶段先返回空串（只验证模板+gradle 这条确定性链路能产出可编译的 jar）。
    具体内容类型的注册代码在 P5 补，注意那部分必须对着真实 jar 的签名写，
    不能凭记忆——Item.Properties 这类 API 每个版本都会变。
    """
    return ''


def render_project(spec: dict, out_dir: str, mirror: str = 'cn') -> list:
    """把 spec 渲染成完整工程，返回相对路径列表。"""
    values = base_values(spec, mirror)
    values['register_calls'] = register_calls(spec)
    pkg_path = values['package_path']
    cls = values['mod_class']
    written = copy_static(out_dir)

    # 顶层构建脚本
    for name in ('settings.gradle', 'build.gradle', 'gradle.properties'):
        write_out(os.path.join(out_dir, name), fill(read_tpl(name), values))
        written.append(name)

    # wrapper 配置（含 gradle 分发源，受 mirror 影响，所以是模板不是静态文件）
    wrapper_rel = os.path.join('gradle', 'wrapper', 'gradle-wrapper.properties')
    write_out(os.path.join(out_dir, wrapper_rel),
              fill(read_tpl(wrapper_rel), values))
    written.append(wrapper_rel)

    # 资源
    write_out(os.path.join(out_dir, 'src', 'main', 'resources', 'fabric.mod.json'),
              fill(read_tpl('fabric.mod.json'), values))
    written.append('src/main/resources/fabric.mod.json')

    # 双源码集入口类
    main_rel = os.path.join('src', 'main', 'java', pkg_path, cls + '.java')
    write_out(os.path.join(out_dir, main_rel),
              fill(read_tpl('ModClass.java'), values))
    written.append(main_rel)

    client_rel = os.path.join('src', 'client', 'java', pkg_path, 'client', cls + 'Client.java')
    write_out(os.path.join(out_dir, client_rel),
              fill(read_tpl('ModClassClient.java'), values))
    written.append(client_rel)

    return written
