"""LLM 调用的最小抽象层。

支持两种请求格式，因为它们覆盖了绝大多数云端服务：
  anthropic —— POST {base}/v1/messages     （官方 Claude，以及 DeepSeek 的 /anthropic 兼容端点）
  openai    —— POST {base}/v1/chat/completions

两种格式的差异只体现在请求构造和响应提取，上层只拿到一段纯文本。
零第三方依赖，HTTP 用 urllib.request。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 180
MAX_RETRIES = 3

# `deepseek-v4-pro[1m]` 里的 [1m] 是 Claude Code 内部的上下文长度标记，
# 不是 API 认识的模型 ID，直接发过去会被拒。用之前必须剥掉。
CONTEXT_SUFFIX = re.compile(r'\[[0-9]+[mk]\]$', re.IGNORECASE)


class LLMError(Exception):
    pass


# ---------- 配置 ----------

def strip_context_suffix(model: str) -> str:
    if not model:
        return model
    return CONTEXT_SUFFIX.sub('', model.strip())


def read_claude_settings() -> dict:
    """从 ~/.claude/settings.json 的 env 块里读用户的现成配置。

    这样用户不用重复填 key —— 他已经在用的那个端点直接就能用。
    读不到就返回空 dict，不算错误。
    """
    path = os.path.expanduser('~/.claude/settings.json')
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    env = data.get('env')
    return env if isinstance(env, dict) else {}


def first_of(*values):
    for v in values:
        if v:
            return v
    return None


def resolve_config(cli_key=None, cli_base=None, cli_model=None, cli_format=None) -> dict:
    """按优先级定出 (format, base_url, api_key, model)。

    优先级：命令行 > 环境变量 > ~/.claude/settings.json > 内置默认。
    注意 ~/.zshrc 里的 DEEPSEEK_API_KEY 可能是没填的占位符，
    所以对明显不是 key 的值（含中文、太短）直接判为无效。
    """
    env = os.environ
    settings_env = read_claude_settings()

    def pick(*names):
        return first_of(*[env.get(n) for n in names],
                        *[settings_env.get(n) for n in names])

    base = first_of(
        cli_base,
        pick('ANTHROPIC_BASE_URL', 'OPENAI_BASE_URL', 'OPENAI_API_BASE'),
    )
    key = first_of(
        cli_key,
        pick('ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY',
             'OPENAI_API_KEY', 'DEEPSEEK_API_KEY'),
    )
    model = first_of(
        cli_model,
        pick('ANTHROPIC_MODEL', 'OPENAI_MODEL'),
    )
    fmt = cli_format or pick('MCGEN_FORMAT')

    if key and not plausible_key(key):
        # 占位符会让请求以 401 失败，不如在这里就点破
        raise LLMError(
            '取到的 API key 看着像占位符而不是真 key（值不合法：长度或字符集不对）。\n'
            '请用 --api-key 显式指定，或填好 ~/.claude/settings.json。'
        )

    if not fmt:
        # 从 base_url 推断：带 /anthropic 的走 anthropic 格式
        if base and 'anthropic' in base.lower():
            fmt = 'anthropic'
        elif base and ('claude' in base.lower() or 'api.anthropic.com' in base):
            fmt = 'anthropic'
        else:
            fmt = 'openai'

    if fmt == 'anthropic':
        base = base or 'https://api.anthropic.com'
        model = model or 'claude-sonnet-4-5'
    else:
        base = base or 'https://api.openai.com/v1'
        model = model or 'gpt-4o'

    return {
        'format': fmt,
        'base_url': base.rstrip('/'),
        'api_key': key,
        'model': strip_context_suffix(model),
    }


def plausible_key(key: str) -> bool:
    """粗筛：真 key 是 ASCII、没有空白、有一定长度。"""
    if len(key) < 16:
        return False
    if any(ord(c) > 127 for c in key):
        return False
    return not any(c.isspace() for c in key)


# ---------- URL ----------

def endpoint(base: str, path: str) -> str:
    """拼端点，避免出现 /v1/v1/ 这种重复。"""
    base = base.rstrip('/')
    if base.endswith('/v1') and path.startswith('/v1/'):
        return base + path[3:]
    return base + path


# ---------- 请求构造 ----------

def build_request(cfg: dict, system: str, user: str,
                  max_tokens: int, temperature: float) -> tuple:
    """返回 (url, headers, body_dict)。两种格式的差异全在这里。"""
    if cfg['format'] == 'anthropic':
        url = endpoint(cfg['base_url'], '/v1/messages')
        headers = {
            'content-type': 'application/json',
            'anthropic-version': '2023-06-01',
        }
        # 官方认 x-api-key，DeepSeek 的兼容端点认 Bearer。两个都发。
        if cfg['api_key']:
            headers['x-api-key'] = cfg['api_key']
            headers['authorization'] = 'Bearer ' + cfg['api_key']
        body = {
            'model': cfg['model'],
            'max_tokens': max_tokens,
            'temperature': temperature,
            'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': user}]}],
        }
        if system:
            body['system'] = system
        return url, headers, body

    url = endpoint(cfg['base_url'], '/v1/chat/completions')
    headers = {'content-type': 'application/json'}
    if cfg['api_key']:
        headers['authorization'] = 'Bearer ' + cfg['api_key']
    messages = []
    if system:
        messages.append({'role': 'system', 'content': system})
    messages.append({'role': 'user', 'content': user})
    body = {
        'model': cfg['model'],
        'messages': messages,
        'temperature': temperature,
        'max_tokens': max_tokens,
    }
    return url, headers, body


def extract_text(cfg: dict, data: dict) -> str:
    """从两种响应格式里把正文抠出来。

    注意模型可能返回多个 content 块：推理模型会先给一个 thinking 块，
    正文在后面的 text 块里。只挑 text 类型，别的都跳过。
    """
    stop_reason = data.get('stop_reason') or ''
    kinds = []

    if cfg['format'] == 'anthropic':
        parts = data.get('content') or []
        kinds = [p.get('type') for p in parts if isinstance(p, dict)]
        texts = [p.get('text', '') for p in parts if isinstance(p, dict)
                 and p.get('type') == 'text']
        text = ''.join(texts)
    else:
        choices = data.get('choices') or []
        if not choices:
            raise LLMError('响应里没有 choices：%s' % json.dumps(data)[:300])
        msg = choices[0].get('message') or {}
        text = msg.get('content') or ''
        stop_reason = stop_reason or choices[0].get('finish_reason') or ''

    if not text.strip():
        if stop_reason == 'max_tokens':
            # 推理模型最容易踩这个：思考过程把预算吃光了，还没开始写正文
            raise LLMError(
                'max_tokens 用完了，模型还没来得及输出正文'
                '（本次收到的内容块类型：%s）。\n'
                '推理模型会先花 token 思考，请把 max_tokens 调大。' % (kinds or '无'))
        raise LLMError('模型返回了空内容（内容块类型：%s，stop_reason=%r）'
                       % (kinds or '无', stop_reason))
    return text


# ---------- 调用 ----------

def llm_chat(cfg: dict, system: str, user: str,
             max_tokens: int = 16000, temperature: float = 0.2,
             timeout: int = DEFAULT_TIMEOUT) -> str:
    """发一次请求，返回正文文本。网络错误退避重试，HTTP 错误直接报出来。"""
    if not cfg.get('api_key'):
        raise LLMError('没有可用的 API key。用 --api-key 指定，或配好环境变量。')

    url, headers, body = build_request(cfg, system, user, max_tokens, temperature)
    payload = json.dumps(body).encode('utf-8')
    last_error = ''

    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode('utf-8', errors='replace')
            return extract_text(cfg, json.loads(raw))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='replace')[:600]
            # 4xx 多半是配置问题，重试没意义，直接抛
            if 400 <= exc.code < 500:
                raise LLMError('HTTP %d：%s' % (exc.code, detail))
            last_error = 'HTTP %d：%s' % (exc.code, detail)
        except urllib.error.URLError as exc:
            last_error = '网络错误：%s' % exc.reason
        except (TimeoutError, OSError) as exc:
            last_error = '连接异常：%s' % exc
        except json.JSONDecodeError as exc:
            raise LLMError('响应不是合法 JSON：%s' % exc)

        if attempt < MAX_RETRIES - 1:
            time.sleep(2 ** attempt)

    raise LLMError('重试 %d 次仍失败：%s' % (MAX_RETRIES, last_error))
