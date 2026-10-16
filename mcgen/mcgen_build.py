"""调用 gradlew 构建工程，并从输出里把 javac 的编译错误抠出来。

只做两件事：把 gradle 跑起来，把错误变成能喂给 LLM 的短文本。
日志里绝大多数是下载进度之类的噪音，不能整段回喂。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time

# keg-only 的 brew JDK 不会进 PATH，得自己找出来塞进 JAVA_HOME
JAVA_HOME_HINTS = [
    '/opt/homebrew/opt/openjdk@25',
    '/usr/local/opt/openjdk@25',
]

# 首次构建要拉 Gradle 分发包、Minecraft、Fabric API 和一大堆插件依赖。
# 实测在跨国网络上可能拖到 20 分钟以上——gradle 在 lifecycle 级别不打印下载进度，
# 中间会有大段完全静默的下载期，所以总超时要给得很宽，不能靠"多久没输出"判死。
FIRST_BUILD_TIMEOUT = 5400
NEXT_BUILD_TIMEOUT = 600

HEARTBEAT_SECONDS = 30


# ---------- 定位 JDK ----------

def find_java_home() -> str:
    """找出 JDK 25 的 JAVA_HOME。找不到返回空串。"""
    # 显式指定优先
    env_home = os.environ.get('JAVA_HOME')
    if env_home and os.path.exists(os.path.join(env_home, 'bin', 'java')):
        return env_home

    for hint in JAVA_HOME_HINTS:
        if os.path.exists(os.path.join(hint, 'bin', 'java')):
            return hint

    # 交给 macOS 的 java_home 工具兜底（用户若装了 .jdk 到标准位置）
    try:
        out = subprocess.run(['/usr/libexec/java_home', '-v', '25'],
                             capture_output=True, text=True, timeout=15)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ''


def build_env() -> dict:
    """构造带 JAVA_HOME 的环境变量。

    强制 C locale：否则中文系统上 javac 会输出「错误:」而非 "error:"，
    错误解析的正则会一条都匹配不到。让 javac 说英文，解析才确定。
    """
    env = os.environ.copy()
    java_home = find_java_home()
    if java_home:
        env['JAVA_HOME'] = java_home
        env['PATH'] = os.path.join(java_home, 'bin') + os.pathsep + env.get('PATH', '')
    env['LANG'] = 'C'
    env['LC_ALL'] = 'C'
    return env


# ---------- 跑构建 ----------

def download_bytes() -> int:
    """统计 gradle 临时下载目录的字节数，用作"还在动"的证据。

    gradle 在 lifecycle 日志级别不打印下载进度，所以长时间的依赖下载期间
    命令行上什么都不会出现。靠这个数字做心跳，用户才知道不是卡死。
    """
    tmp = os.path.join(os.path.expanduser('~'), '.gradle', '.tmp')
    total = 0
    try:
        for name in os.listdir(tmp):
            try:
                total += os.path.getsize(os.path.join(tmp, name))
            except OSError:
                pass
    except OSError:
        pass
    return total


def run_build(project_dir: str, first: bool = False, timeout: int = 0) -> tuple:
    """跑 ./gradlew build，实时把输出透出去，同时累积供错误解析。

    返回 (是否成功, 完整输出)。

    两个关键点：
      --console=plain  让输出稳定可解析
      流式输出         绝不能 capture_output —— 首次构建要跑十几分钟，
                       全程无输出会被当成挂死（这是实测踩过的坑）
    """
    if not timeout:
        timeout = FIRST_BUILD_TIMEOUT if first else NEXT_BUILD_TIMEOUT

    cmd = ['./gradlew', 'build', '--console=plain', '--no-configuration-cache',
           '--stacktrace']
    try:
        proc = subprocess.Popen(
            cmd, cwd=project_dir, env=build_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors='ignore', bufsize=1)
    except OSError as exc:
        return False, '无法执行 gradlew：%s' % exc

    chunks = []
    started = time.time()
    done = threading.Event()

    def pump():
        for line in proc.stdout:
            chunks.append(line)
            sys.stdout.write(line)
            sys.stdout.flush()
        done.set()

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()

    base = download_bytes()
    last_beat = time.time()
    try:
        while proc.poll() is None:
            time.sleep(1)
            if done.is_set():
                break
            now = time.time()
            if now - started > timeout:
                proc.kill()
                return False, ''.join(chunks) + '\n构建超时（超过 %d 秒）' % timeout
            if now - last_beat >= HEARTBEAT_SECONDS:
                last_beat = now
                grew = download_bytes() - base
                print('      ...已跑 %d 分钟，下载累计 %.1f MB'
                      % ((now - started) // 60, grew / 1048576.0))
    except KeyboardInterrupt:
        proc.kill()
        raise

    thread.join(timeout=10)
    return proc.returncode == 0, ''.join(chunks)


# ---------- 提取错误 ----------

# javac 的错误行：/path/File.java:12: error: cannot find symbol
# 兼容中文 locale 的「错误:」作兜底（虽然 build_env 已强制 LC_ALL=C）
ERROR_HEAD = re.compile(
    r'^(?P<file>[^\s:]+\.java):(?P<line>\d+):\s*(?P<kind>error|warning|错误|警告):\s*(?P<msg>.*)$')

# 这些行没有信息量，回喂给 LLM 只是浪费 token
NOISE = re.compile(
    r'^(Download|Downloading|> Task |BUILD |FAILURE:|Welcome to Gradle|'
    r'Starting a Gradle Daemon|Deprecated|Note:|Daemon|Configuration cache|'
    r'Calculating task graph|\s*$)')

# 依赖下载失败的特征。这类失败没有 javac 错误行，但把 Java 堆栈当编译错误
# 打印出来对用户毫无用处——所以得单独认出来，给人话。
DEPENDENCY_FAILURE = re.compile(
    r'Could not (?:download|resolve|get resource)|Read timed out|'
    r'Connection reset|UnknownHostException|Could not GET')


def summarize_dependency_failure(lines: list) -> str:
    """把依赖下载失败摘成一段人话，而不是把 Java 堆栈糊上去。

    实测：跨国拉 maven.fabricmc.net 只有 4~27 KB/s，gradle 默认 30 秒 socket 超时
    会在下载中途报 Read timed out。这跟代码毫无关系，混在"编译错误"里会误导人。
    """
    start = None
    for idx, raw in enumerate(lines):
        s = raw.strip()
        if DEPENDENCY_FAILURE.search(s) and not s.startswith('at '):
            start = idx
            break
    if start is None:
        return ''

    out = []
    for raw in lines[start:start + 30]:
        s = raw.strip()
        if s.startswith('at ') or s.startswith('...'):
            continue                                  # 堆栈行，丢掉
        if not s:
            if out:
                break
            continue
        if s.startswith('>') or s.startswith('* '):
            out.append(s)
        elif out:
            break
        if len(out) >= 12:
            break

    return ('依赖下载失败（不是代码问题）。maven.fabricmc.net 这条链路很慢，'
            '构建里已放宽 socket 超时，直接重试通常就能过：\n' + '\n'.join(out))


def extract_errors(output: str, max_errors: int = 15, max_chars: int = 4000) -> str:
    """从 gradle 输出里抽出编译错误，返回适合塞进 prompt 的短文本。

    每条错误保留错误行和紧随其后的几行上下文——javac 会把 "符号: 类 XXX"
    这类关键信息放在后续行里，只留错误首行等于把最有用的部分丢了。
    """
    lines = output.splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        m = ERROR_HEAD.match(lines[i].strip())
        if not m:
            i += 1
            continue
        block = [lines[i].strip()]
        # 收集后续上下文，直到下一个错误或累计 4 行
        j = i + 1
        while j < len(lines) and len(block) < 4:
            nxt = lines[j].strip()
            if ERROR_HEAD.match(nxt) or nxt.startswith('> Task'):
                break
            if nxt and not NOISE.match(nxt):
                block.append('\t' + nxt)
            j += 1
        blocks.append('\n'.join(block))
        i = j
        if len(blocks) >= max_errors:
            break

    if not blocks:
        dep = summarize_dependency_failure(lines)
        if dep:
            return dep[:max_chars]
        # 没有 javac 错误行，可能是配置阶段就挂了。
        # 这时保留 FAILURE 段比返回空更有用。
        tail = [l for l in lines if not NOISE.match(l)][-30:]
        return '\n'.join(tail)[:max_chars]

    text = '\n\n'.join(blocks)
    if len(blocks) >= max_errors:
        text += '\n\n（错误过多，已截断到前 %d 条）' % max_errors
    return text[:max_chars]
