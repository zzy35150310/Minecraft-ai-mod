"""从编译好的 jar 里提取类的真实 API 签名，供 LLM 上下文使用。

Minecraft 自 26.1 起不再混淆，jar 里就是可读的真实符号名
（`net.minecraft.resources.Identifier`，而不是 1.21 时代的 `ResourceLocation` 映射名）。
所以不需要 javap、不需要 `gradlew genSources`：直接读 class 文件的常量池与方法表，
就能拿到准确的签名。这让"给 LLM 喂真实 API"变成零依赖、秒级的确定性操作，
而不是一次要反编译整个 Minecraft、还可能卡死的重活。

读 jar 需要 JDK 吗？不需要——纯 Python 解 zip 和 class 文件格式。

用法：
    python3 mcgen_apis.py <jar> <类名> [<类名> ...]
    python3 mcgen_apis.py --list <jar> <关键词>      # 按关键词搜类名
    python3 mcgen_apis.py --grep <jar> <方法名>      # 全 jar 搜方法名
"""

from __future__ import annotations

import struct
import sys
import zipfile

# 常量池各 tag 的载荷长度。值 5/6（Long/Double）占两个槽位，要单独处理。
CP_FIXED = {3: 4, 4: 4, 7: 2, 8: 2, 9: 4, 10: 4, 11: 4, 12: 4,
            15: 3, 16: 2, 17: 4, 18: 4, 19: 2, 20: 2}

# 基本类型描述符 → Java 里的写法
BASE_TYPES = {'B': 'byte', 'C': 'char', 'D': 'double', 'F': 'float',
              'I': 'int', 'J': 'long', 'S': 'short', 'Z': 'boolean', 'V': 'void'}


# ---------- class 文件解析 ----------

def _skip_attributes(data: bytes, pos: int, count: int) -> int:
    """跳过 count 个 attribute_info，返回新偏移。"""
    for _ in range(count):
        pos += 2                                   # attribute_name_index
        length = struct.unpack_from('>I', data, pos)[0]
        pos += 4 + length
    return pos


def parse_class(data: bytes) -> dict:
    """把 class 文件的常量池、字段表、方法表读出来。

    只解析到方法表为止——方法体（Code 属性）整个跳过，
    因为我们只要签名，不要字节码。
    """
    if data[:4] != b'\xca\xfe\xba\xbe':
        raise ValueError('不是 class 文件（magic 不对）')

    pos = 8                                        # magic(4) + minor(2) + major(2)
    count = struct.unpack_from('>H', data, pos)[0]
    pos += 2

    cp = [None] * count
    i = 1
    while i < count:
        tag = data[pos]
        pos += 1
        if tag == 1:                               # Utf8
            length = struct.unpack_from('>H', data, pos)[0]
            pos += 2
            cp[i] = data[pos:pos + length].decode('utf-8', 'replace')
            pos += length
        elif tag in (5, 6):                        # Long / Double：占两个槽
            cp[i] = struct.unpack_from('>q' if tag == 5 else '>d', data, pos)[0]
            pos += 8
            i += 1
        elif tag == 15:                            # MethodHandle
            pos += 3
        elif tag in CP_FIXED:
            # unpack_from 一律返回元组，两字节的条目要取出标量，
            # 否则后面解引用 this_class 时会当成"不是下标"而失败
            size = CP_FIXED[tag]
            if size == 2:
                cp[i] = struct.unpack_from('>H', data, pos)[0]
            else:
                cp[i] = struct.unpack_from('>HH', data, pos)
            pos += size
        else:
            raise ValueError('无法识别的常量池 tag：%d（偏移 %d）' % (tag, pos - 1))
        i += 1

    access = struct.unpack_from('>H', data, pos)[0]
    pos += 2
    this_idx = struct.unpack_from('>H', data, pos)[0]
    pos += 2
    super_idx = struct.unpack_from('>H', data, pos)[0]
    pos += 2

    n_iface = struct.unpack_from('>H', data, pos)[0]
    pos += 2 + n_iface * 2

    # 字段：读名字与类型，但签名检索用不上字段的元数据细节
    n_fields = struct.unpack_from('>H', data, pos)[0]
    pos += 2
    fields = []
    for _ in range(n_fields):
        f_access, name_idx, desc_idx = struct.unpack_from('>HHH', data, pos)
        pos += 6
        n_attr = struct.unpack_from('>H', data, pos)[0]
        pos = _skip_attributes(data, pos + 2, n_attr)
        fields.append((f_access, cp[name_idx], cp[desc_idx]))

    n_methods = struct.unpack_from('>H', data, pos)[0]
    pos += 2
    methods = []
    for _ in range(n_methods):
        m_access, name_idx, desc_idx = struct.unpack_from('>HHH', data, pos)
        pos += 6
        n_attr = struct.unpack_from('>H', data, pos)[0]
        pos = _skip_attributes(data, pos + 2, n_attr)
        methods.append((m_access, cp[name_idx], cp[desc_idx]))

    # this_class / super_class 存的是常量池下标，指向 Class 条目，
    # 而 Class 条目本身又指向一个 Utf8 —— 所以是两层解引用。
    def class_name(idx):
        if not idx:
            return None
        ref = cp[idx]
        return cp[ref] if isinstance(ref, int) else None

    return {
        'name': class_name(this_idx),
        'super': class_name(super_idx),
        'access': access,
        'methods': methods,
        'fields': fields,
    }


# ---------- 描述符 → 可读签名 ----------

def parse_type(desc: str, i: int) -> tuple:
    """从 desc 的第 i 位读一个类型，返回 (可读名, 新位置)。"""
    dim = 0
    while i < len(desc) and desc[i] == '[':
        dim += 1
        i += 1
    ch = desc[i]
    if ch == 'L':
        end = desc.index(';', i)
        name = desc[i + 1:end].replace('/', '.')
        i = end + 1
    else:
        name = BASE_TYPES.get(ch, ch)
        i += 1
    return name + '[]' * dim, i


def pretty_type(desc: str) -> str:
    """把单个类型的描述符翻成可读写法：Ljava/lang/String; -> java.lang.String"""
    return parse_type(desc, 0)[0]


def split_method(desc: str) -> tuple:
    """把方法描述符拆成 (返回类型, 参数列表)。

    (Ljava/lang/String;I)V  ->  ('void', 'java.lang.String, int')

    返回类型必须和方法名分开渲染，否则会拼出 `assertTruevoid (boolean)`
    这种既不像 Java 又难读的东西。LLM 对 JVM 描述符的把握很差，
    喂可读形式能显著减少签名错误。
    """
    if not desc.startswith('('):
        return pretty_type(desc), ''
    i = 1
    args = []
    while i < len(desc) and desc[i] != ')':
        t, i = parse_type(desc, i)
        args.append(t)
    ret, _ = parse_type(desc, i + 1)
    return ret, ', '.join(args)


def flags_text(access: int) -> str:
    """方法/字段的修饰符。只保留对写代码有影响的几个。"""
    bits = []
    if access & 0x0001:
        bits.append('public')
    if access & 0x0002:
        bits.append('private')
    if access & 0x0004:
        bits.append('protected')
    if access & 0x0008:
        bits.append('static')
    if access & 0x0010:
        bits.append('final')
    if access & 0x0400:
        bits.append('abstract')
    return ' '.join(bits)


def format_api(info: dict, with_fields: bool = False) -> str:
    """把一个类的 API 渲染成给 LLM 看的短文本。"""
    head = 'class %s' % (info['name'] or '?').replace('/', '.')
    if info.get('super'):
        head += ' extends %s' % info['super'].replace('/', '.')
    lines = [head]

    if with_fields:
        for access, name, desc in info['fields']:
            if name.startswith('this$') or access & 0x1000:   # 合成字段，没意义
                continue
            lines.append('  %s %s %s' % (flags_text(access),
                                         pretty_type(desc), name))

    simple = (info['name'] or '?').rsplit('/', 1)[-1]
    seen = set()
    for access, name, desc in info['methods']:
        if access & 0x1000:                            # 合成桥接方法，跳过
            continue
        if name == '<clinit>':
            continue
        ret, args = split_method(desc)
        if name == '<init>':
            # 构造函数的字节码名是 <init>，且 Java 里构造器不写返回类型
            sig = '%s %s(%s)' % (flags_text(access), simple, args)
        else:
            sig = '%s %s %s(%s)' % (flags_text(access), ret, name, args)
        if sig in seen:
            continue
        seen.add(sig)
        lines.append('  ' + sig)
    return '\n'.join(lines)


# ---------- 读 jar ----------

def class_entries(jar_path: str) -> list:
    """列出 jar 里所有类名（点分形式，不含 .class 后缀）。"""
    with zipfile.ZipFile(jar_path) as zf:
        names = [n[:-6] for n in zf.namelist()
                 if n.endswith('.class') and not n.startswith('META-INF/')]
    return [n.replace('/', '.') for n in names]


def read_class(jar_path: str, class_name: str) -> dict:
    """从 jar 里读一个类并解析。class_name 用点分或斜杠形式都行。"""
    entry = class_name.replace('.', '/') + '.class'
    with zipfile.ZipFile(jar_path) as zf:
        try:
            data = zf.read(entry)
        except KeyError:
            # 内嵌类：允许调用方只给外部类名
            raise KeyError('jar 里没有 %s' % class_name)
    return parse_class(data)


def api_of(jar_path: str, class_name: str, with_fields: bool = False) -> str:
    """一步到位：读类 + 渲染，返回可直接塞 prompt 的文本。"""
    return format_api(read_class(jar_path, class_name), with_fields)


def grep_methods(jar_path: str, needle: str, limit: int = 40) -> str:
    """全 jar 搜方法名，返回「类名 + 签名」清单。

    用于"我只知道大概叫什么、不确定在哪个类"的场合。
    """
    needle_lower = needle.lower()
    hits = []
    with zipfile.ZipFile(jar_path) as zf:
        for entry in zf.namelist():
            if not entry.endswith('.class') or entry.startswith('META-INF/'):
                continue
            try:
                info = parse_class(zf.read(entry))
            except (ValueError, struct.error, IndexError):
                continue
            for access, name, desc in info['methods']:
                if needle_lower in name.lower():
                    ret, args = split_method(desc)
                    hits.append('%s  →  %s %s %s(%s)'
                                % ((info['name'] or entry[:-6]).replace('/', '.'),
                                   flags_text(access), ret, name, args))
                    if len(hits) >= limit:
                        return '\n'.join(hits) + '\n（已截断到 %d 条）' % limit
    return '\n'.join(hits) if hits else '（没有匹配的方法）'


# ---------- 命令行 ----------

def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    if sys.argv[1] == '--list':
        for name in sorted(class_entries(sys.argv[2])):
            if len(sys.argv) > 3:
                if sys.argv[3].lower() not in name.lower():
                    continue
            print(name)
        return

    if sys.argv[1] == '--grep':
        if len(sys.argv) < 4:
            print('用法：python3 mcgen_apis.py --grep <jar> <方法名>')
            sys.exit(1)
        print(grep_methods(sys.argv[2], sys.argv[3]))
        return

    jar = sys.argv[1]
    for name in sys.argv[2:]:
        print(api_of(jar, name))
        print()


if __name__ == '__main__':
    main()
