"""不依赖 JDK 和网络的纯逻辑自测。

跑：python3 selftest.py
用的是真实形态的 gradle/javac 输出作为样本——这个解析逻辑很脆，
不上测试的话，改动一次就可能静默失效（返回空串，然后 LLM 拿到空气去修 bug）。
"""

from __future__ import annotations

import struct
import sys

import mcgen_apis
import mcgen_build
import mcgen_llm
import mcgen_render
import mcgen_spec

FAILED = []


def check(name: str, got, want):
    if got != want:
        FAILED.append('%s\n  期望: %r\n  实际: %r' % (name, want, got))
        print('  FAIL  %s' % name)
    else:
        print('  ok    %s' % name)


def contains(name: str, haystack: str, needle: str):
    if needle not in haystack:
        FAILED.append('%s\n  输出里找不到: %r\n  实际: %r' % (name, needle, haystack))
        print('  FAIL  %s' % name)
    else:
        print('  ok    %s' % name)


# ---------- 错误解析 ----------

GRADLE_FAILURE = """Starting a Gradle Daemon (subsequent builds will be faster)

> Configure project :
Fabric Loom: 1.18.20

> Task :compileJava FAILED
/Users/zzy/out/src/main/java/com/example/testmod/Testmod.java:12: error: cannot find symbol
\tpublic static final Item BOOM = new Item(new Item.Properties().setId(id("boom")));
\t                                          ^
\t  symbol:   method setId(Identifier)
\t  location: class net.minecraft.world.item.Item.Properties
/Users/zzy/out/src/main/java/com/example/testmod/Testmod.java:15: error: incompatible types: int cannot be converted to String
\tString s = 3;

2 errors

FAILURE: Build failed with an exception.

* What went wrong:
Execution failed for task ':compileJava'.
"""


def test_extract_errors():
    out = mcgen_build.extract_errors(GRADLE_FAILURE)
    contains('错误：抓到第一条 error', out, 'cannot find symbol')
    contains('错误：抓到符号上下文', out, 'symbol:   method setId')
    contains('错误：抓到第二条 error', out, 'incompatible types')
    contains('错误：带上行号', out, ':12:')
    if 'Downloading' in out or 'Starting a Gradle Daemon' in out:
        FAILED.append('噪音没滤干净')
        print('  FAIL  错误：滤除噪音')
    else:
        print('  ok    错误：滤除噪音')


CHINESE_LOCALE = """\
> Task :compileJava FAILED
/Users/zzy/out/A.java:3: 错误: 找不到符号
\tItem x = q;
\t         ^
"""


def test_chinese_locale():
    out = mcgen_build.extract_errors(CHINESE_LOCALE)
    contains('中文 locale 兜底', out, '找不到符号')


NO_JAVAC_ERRORS = """\
FAILURE: Build failed with an exception.

* Where:
Build file '/tmp/x/build.gradle' line: 12

* What went wrong:
A problem occurred evaluating root project 'testmod'.
> startup failed: unexpected token: }
"""


def test_config_phase_failure():
    out = mcgen_build.extract_errors(NO_JAVAC_ERRORS)
    contains('非 javac 失败：保留 FAILURE 内容', out, 'unexpected token')


# 这次真实踩到的构建失败（8 分 26 秒后崩掉），原样留作样本
DEPENDENCY_FAILURE_SAMPLE = """\
> Task :compileJava

FAILURE: Build failed with an exception.

* What went wrong:
> Could not resolve all artifacts for configuration 'classpath'.
   > Could not download fabric-loom-1.18.4.jar (net.fabricmc:fabric-loom:1.18.4)
      > Could not get resource 'https://maven.fabricmc.net/net/fabricmc/fabric-loom/1.18.4/fabric-loom-1.18.4.jar'.
         > Read timed out
   > Could not download mercury-0.4.3.jar (net.fabricmc:mercury:0.4.3)
      > Read timed out

* Try:
Run with --stacktrace option to get the stack trace.
\tat java.base/sun.nio.ch.NioSocketImpl.implRead(NioSocketImpl.java:302)
\tat java.base/sun.nio.ch.NioSocketImpl.read(NioSocketImpl.java:354)
\tat org.gradle.internal.resource.transfer.DownloadAction.execute(DownloadAction.java:59)
"""


def test_dependency_failure():
    out = mcgen_build.extract_errors(DEPENDENCY_FAILURE_SAMPLE)
    contains('下载失败: 认出是依赖问题', out, '依赖下载失败')
    contains('下载失败: 带上具体 artifact', out, 'fabric-loom-1.18.4.jar')
    contains('下载失败: 带上真实原因', out, 'Read timed out')
    if 'at java.base/' in out or 'DownloadAction' in out:
        FAILED.append('下载失败的输出里不该出现 Java 堆栈')
        print('  FAIL  下载失败: 滤掉堆栈')
    else:
        print('  ok    下载失败: 滤掉堆栈')


# ---------- modid 归一化 ----------

def test_normalize():
    check('modid: 空格与大小写', mcgen_spec.normalize_modid('Boom Sword'), 'boom_sword')
    check('modid: 连字符', mcgen_spec.normalize_modid('my-cool-mod'), 'my_cool_mod')
    check('modid: 数字开头补前缀', mcgen_spec.normalize_modid('3d_thing'), 'mod_3d_thing')
    check('modid: 中文全被剔掉', mcgen_spec.normalize_modid('爆炸剑'), 'mod_')
    check('modid: 超长截断', len(mcgen_spec.normalize_modid('a' * 200)), 64)


# ---------- 类名 ----------

def test_class_name():
    check('类名: 下划线转驼峰', mcgen_render.mod_class_name('boom_sword'), 'BoomSword')
    check('类名: 单个词', mcgen_render.mod_class_name('testmod'), 'Testmod')
    check('类名: 连字符', mcgen_render.mod_class_name('my-mod'), 'MyMod')


# ---------- 校验 ----------

def test_validate():
    good = {'modid': 'testmod', 'package': 'com.example.testmod',
            'content': [{'type': 'item', 'id': 'boom_sword'}]}
    check('校验: 合法 spec 通过', mcgen_spec.validate(good), [])

    bad_modid = dict(good, modid='BoomSword')
    check('校验: 拒绝大写 modid', len(mcgen_spec.validate(bad_modid)), 1)

    bad_type = dict(good, content=[{'type': 'dimension', 'id': 'x'}])
    check('校验: 拒绝白名单外的类型', len(mcgen_spec.validate(bad_type)), 1)

    dup = dict(good, content=[{'type': 'item', 'id': 'a'}, {'type': 'item', 'id': 'a'}])
    check('校验: 拒绝重复 id', len(mcgen_spec.validate(dup)), 1)

    check('校验: 未实现的类型被识别',
          mcgen_spec.unsupported({'content': [{'type': 'block', 'id': 'b'}]}), ['block'])


def test_defaults():
    spec = mcgen_spec.apply_defaults({'name': 'Boom Sword'})
    check('默认值: modid 从 name 推导', spec['modid'], 'boom_sword')
    check('默认值: package 从 modid 推导', spec['package'], 'com.example.boom_sword')
    check('默认值: version', spec['version'], '1.0.0')


# ---------- 需求 → spec ----------

GOOD_SPEC_JSON = ('{"modid":"boomsword","name":"爆炸剑","content":'
                  '[{"type":"item","id":"boom_sword","name":"爆炸钻石剑",'
                  '"max_stack":1}]}')
# type 不在白名单内，校验必须拦下
BAD_TYPE_JSON = '{"modid":"boomsword","content":[{"type":"dimension","id":"d"}]}'


def scripted(replies: list, log: list):
    """造一个假 chat：按脚本依次返回，并把收到的 prompt 记进 log。

    有了它，重试循环可以完全脱网测试——而这正是最容易写错的逻辑。
    """
    state = {'i': 0}

    def chat(system, user):
        log.append(user)
        idx = min(state['i'], len(replies) - 1)
        state['i'] += 1
        return replies[idx]
    return chat


def test_extract_json():
    check('JSON: 裸 JSON',
          mcgen_spec.extract_json(GOOD_SPEC_JSON)['modid'], 'boomsword')
    check('JSON: markdown 围栏',
          mcgen_spec.extract_json('```json\n%s\n```' % GOOD_SPEC_JSON)['modid'],
          'boomsword')
    check('JSON: 前后有废话',
          mcgen_spec.extract_json('好的，这是规格：\n%s\n希望有帮助！'
                                  % GOOD_SPEC_JSON)['modid'], 'boomsword')
    try:
        mcgen_spec.extract_json('完全没有 JSON')
        FAILED.append('无 JSON 时应当报错')
        print('  FAIL  JSON: 无 JSON 时报错')
    except ValueError:
        print('  ok    JSON: 无 JSON 时报错')


def test_spec_from_request():
    spec = mcgen_spec.spec_from_request(scripted([GOOD_SPEC_JSON], []), '加一把爆炸剑')
    check('生成: 首次即通过', spec['modid'], 'boomsword')
    check('生成: 自动补 package', spec['package'], 'com.example.boomsword')
    check('生成: 自动补 version', spec['version'], '1.0.0')

    # 第一次类型非法，第二次改正 —— 应当把问题回喂后成功
    log = []
    spec = mcgen_spec.spec_from_request(
        scripted([BAD_TYPE_JSON, GOOD_SPEC_JSON], log), '加一把爆炸剑')
    check('生成: 校验失败后重试成功', spec['modid'], 'boomsword')
    check('生成: 重试时把问题回喂了', 'dimension' in log[-1], True)
    check('生成: 首次请求不带反馈', '上一次' in log[0], False)
    check('生成: 重试请求带反馈', '上一次' in log[-1], True)

    # 一直不合法 —— 必须耗尽重试后明确报错，不能假装成功
    try:
        mcgen_spec.spec_from_request(scripted([BAD_TYPE_JSON], []),
                                     '加个维度', max_retries=1)
        FAILED.append('反复不合法时应当报错')
        print('  FAIL  生成: 耗尽重试后报错')
    except ValueError:
        print('  ok    生成: 耗尽重试后报错')

    # 顶层不是对象（模型爱返回数组）
    try:
        mcgen_spec.spec_from_request(scripted(['[1,2,3]'], []), '随便', max_retries=0)
        FAILED.append('顶层非对象时应当报错')
        print('  FAIL  生成: 顶层非对象报错')
    except ValueError:
        print('  ok    生成: 顶层非对象报错')


# ---------- 镜像 ----------

def test_mirror():
    contains('镜像: cn 用腾讯源',
             mcgen_render.dist_url('cn'), 'mirrors.cloud.tencent.com')
    contains('镜像: official 用官方源',
             mcgen_render.dist_url('official'), 'services.gradle.org')
    contains('镜像: properties 里冒号已转义',
             mcgen_render.dist_url('cn'), 'https\\://')
    # 只镜像 gradle 分发包，不镜像 maven 仓库。
    # 配 aliyun 会卡死（实测堆了一堆 CLOSE_WAIT 到 39.91.185.84），
    # 因为大头是 Fabric maven 与 Mojang，aliyun 并不镜像这些。
    check('镜像: cn 不配 maven 仓库', len(mcgen_render.MAVEN_MIRRORS['cn']), 0)
    check('镜像: official 不带额外仓库', len(mcgen_render.MAVEN_MIRRORS['official']), 0)
    check('镜像: 空仓库渲染成空串', mcgen_render.maven_repo_block('cn', '\t'), '')


# ---------- LLM provider ----------

def test_llm():
    check('模型名: 剥掉 [1m]',
          mcgen_llm.strip_context_suffix('deepseek-v4-pro[1m]'), 'deepseek-v4-pro')
    check('模型名: 剥掉 [1M]',
          mcgen_llm.strip_context_suffix('deepseek-flash[1M]'), 'deepseek-flash')
    check('模型名: 无后缀不动',
          mcgen_llm.strip_context_suffix('gpt-4o'), 'gpt-4o')

    check('URL: anthropic 端点',
          mcgen_llm.endpoint('https://api.deepseek.com/anthropic', '/v1/messages'),
          'https://api.deepseek.com/anthropic/v1/messages')
    check('URL: 不重复 /v1',
          mcgen_llm.endpoint('https://api.openai.com/v1', '/v1/chat/completions'),
          'https://api.openai.com/v1/chat/completions')
    check('URL: 裸 base 补 /v1',
          mcgen_llm.endpoint('https://api.deepseek.com', '/v1/chat/completions'),
          'https://api.deepseek.com/v1/chat/completions')

    check('key 校验: 拒绝中文占位符',
          mcgen_llm.plausible_key('sk-在这里填你的Key'), False)
    check('key 校验: 拒绝过短', mcgen_llm.plausible_key('abc'), False)
    check('key 校验: 接受正常 key',
          mcgen_llm.plausible_key('sk-' + 'a' * 32), True)

    # 请求构造：两种格式的差异
    anthropic = {'format': 'anthropic', 'base_url': 'https://api.deepseek.com/anthropic',
                 'api_key': 'k' * 20, 'model': 'm'}
    url, headers, body = mcgen_llm.build_request(anthropic, 'sys', 'hi', 100, 0.2)
    check('anthropic: 带 anthropic-version',
          headers.get('anthropic-version'), '2023-06-01')
    check('anthropic: 同时带 x-api-key 与 Bearer',
          ('x-api-key' in headers, 'authorization' in headers), (True, True))
    check('anthropic: system 是顶层字段', body.get('system'), 'sys')

    openai = {'format': 'openai', 'base_url': 'https://api.openai.com/v1',
              'api_key': 'k' * 20, 'model': 'm'}
    url, headers, body = mcgen_llm.build_request(openai, 'sys', 'hi', 100, 0.2)
    check('openai: system 进 messages', body['messages'][0]['role'], 'system')
    check('openai: 无 anthropic-version',
          'anthropic-version' in headers, False)

    # 响应提取
    check('提取: anthropic 格式',
          mcgen_llm.extract_text(anthropic,
                                 {'content': [{'type': 'text', 'text': 'ok'}]}), 'ok')
    check('提取: openai 格式',
          mcgen_llm.extract_text(openai,
                                 {'choices': [{'message': {'content': 'ok'}}]}), 'ok')


# ---------- class 文件解析 ----------

def build_minimal_class() -> bytes:
    """手搓一个最小合法 class 文件。

    刻意不依赖外部 jar —— 否则这个测试在没跑过 gradle 的机器上就废了。
    结构：class Test extends java.lang.Object，带一个构造函数和一个静态方法。
    """
    def utf8(s):
        raw = s.encode()
        return b'\x01' + struct.pack('>H', len(raw)) + raw

    def cls(idx):
        return b'\x07' + struct.pack('>H', idx)

    cp = (utf8('Test') + cls(1)                            # 1, 2
          + utf8('java/lang/Object') + cls(3)              # 3, 4
          + utf8('<init>') + utf8('()V')                   # 5, 6
          + utf8('greet') + utf8('(Ljava/lang/String;I)Z'))  # 7, 8

    body = struct.pack('>HHH', 0x0021, 2, 4)               # access / this / super
    body += struct.pack('>H', 0)                           # interfaces
    body += struct.pack('>H', 0)                           # fields
    body += struct.pack('>H', 2)                           # methods
    body += struct.pack('>HHHH', 0x0001, 5, 6, 0)          # public Test()
    body += struct.pack('>HHHH', 0x0009, 7, 8, 0)          # public static boolean greet(...)
    body += struct.pack('>H', 0)                           # class attributes

    return (b'\xca\xfe\xba\xbe' + struct.pack('>HH', 0, 65)
            + struct.pack('>H', 9) + cp + body)


def test_apis():
    check('描述符: 基本类型', mcgen_apis.pretty_type('I'), 'int')
    check('描述符: 对象类型', mcgen_apis.pretty_type('Ljava/lang/String;'),
          'java.lang.String')
    check('描述符: 数组', mcgen_apis.pretty_type('[[I'), 'int[][]')
    check('描述符: 方法拆分',
          mcgen_apis.split_method('(Ljava/lang/String;I)Z'),
          ('boolean', 'java.lang.String, int'))
    check('描述符: 无参方法', mcgen_apis.split_method('()V'), ('void', ''))

    info = mcgen_apis.parse_class(build_minimal_class())
    check('class: 解析出类名', info['name'], 'Test')
    check('class: 解析出父类', info['super'], 'java/lang/Object')
    check('class: 方法数', len(info['methods']), 2)

    text = mcgen_apis.format_api(info)
    contains('class: 类声明', text, 'class Test extends java.lang.Object')
    contains('class: 静态方法签名', text,
             'public static boolean greet(java.lang.String, int)')
    contains('class: 构造函数不带返回类型', text, 'public Test()')

    # magic 不对必须报错，不能静默解析出垃圾喂给 LLM
    try:
        mcgen_apis.parse_class(b'not a class file at all')
        FAILED.append('非法 class 文件应当报错')
        print('  FAIL  class: 拒绝非法文件')
    except ValueError:
        print('  ok    class: 拒绝非法文件')


def main():
    print('错误解析')
    test_extract_errors()
    test_chinese_locale()
    test_config_phase_failure()
    test_dependency_failure()
    print('modid 归一化')
    test_normalize()
    print('类名')
    test_class_name()
    print('spec 校验')
    test_validate()
    test_defaults()
    print('需求 → spec')
    test_extract_json()
    test_spec_from_request()
    print('镜像')
    test_mirror()
    print('LLM provider')
    test_llm()
    print('class 文件解析')
    test_apis()

    print()
    if FAILED:
        print('%d 项失败：' % len(FAILED))
        for f in FAILED:
            print('  - ' + f)
        sys.exit(1)
    print('全部通过')


if __name__ == '__main__':
    main()
