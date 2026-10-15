"""Read-only audit of Snegopat .v named exports. Does not load native DLLs.

This checks exported names, not vtable slots, object layouts or calling safety.
Supports the version/x86/x64 condition syntax used in the Alpha19 API files.
"""
import argparse
import ast
import json
import os
import re
import struct
from pathlib import Path


def version_number(value):
    parts = [int(p) for p in value.split('.')]
    if len(parts) > 4 or any(p > 65535 for p in parts):
        raise ValueError(value)
    parts += [0] * (4 - len(parts))
    return sum(p << (16 * (3 - i)) for i, p in enumerate(parts))


def condition(expression, version, x86):
    expression = expression.split('//')[0]
    expression = re.sub(r'\b\d+(?:\.\d+)+\b', lambda m: str(version_number(m[0])), expression)
    expression = re.sub(r'\bver\b', str(version), expression)
    expression = re.sub(r'\bx86\b', str(int(x86)), expression)
    expression = re.sub(r'\bx64\b', str(int(not x86)), expression)
    expression = expression.replace('&', ' and ').replace('|', ' or ')
    expression = re.sub(r'(?<![<>=!])=(?!=)', '==', expression)
    tree = ast.parse(expression.strip(), mode='eval')
    allowed = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.Compare,
               ast.Constant, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)
    if any(not isinstance(node, allowed) for node in ast.walk(tree)):
        raise ValueError(f'Unsupported condition: {expression}')
    return bool(eval(compile(tree, '<version condition>', 'eval'), {'__builtins__': {}}))


def active_lines(path, version, x86):
    stack = []
    active = True
    raw = path.read_bytes()
    try:
        source = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        source = raw.decode('cp1251')
    for number, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith('#if '):
            result = condition(stripped[4:], version, x86)
            stack.append([active, result])
            active = active and result
        elif stripped.startswith('#elif '):
            parent, taken = stack[-1]
            result = condition(stripped[6:], version, x86)
            active = parent and not taken and result
            stack[-1][1] = taken or result
        elif stripped == '#else':
            parent, taken = stack[-1]
            active = parent and not taken
            stack[-1][1] = True
        elif stripped == '#endif':
            active = stack.pop()[0]
        elif active:
            yield number, stripped
    if stack:
        raise ValueError(f'Unclosed condition in {path}')


def pe_exports(path):
    data = path.read_bytes()
    def word(offset):
        return struct.unpack_from('<H', data, offset)[0]
    def dword(offset):
        return struct.unpack_from('<I', data, offset)[0]
    if data[:2] != b'MZ':
        raise ValueError(f'Not PE: {path}')
    pe = dword(0x3c)
    if data[pe:pe + 4] != b'PE\0\0':
        raise ValueError(f'Invalid PE: {path}')
    optional = pe + 24
    magic = word(optional)
    if magic not in (0x10b, 0x20b):
        raise ValueError(f'Unsupported PE: {path}')
    directories = optional + (96 if magic == 0x10b else 112)
    sections = optional + word(pe + 20)
    headers_size = dword(optional + 60)
    def offset(rva):
        if rva < headers_size:
            return rva
        for index in range(word(pe + 6)):
            section = sections + index * 40
            size, address, raw_size, raw = struct.unpack_from('<IIII', data, section + 8)
            if address <= rva < address + max(size, raw_size):
                return raw + rva - address
        raise ValueError(f'Invalid RVA {rva:x}: {path}')
    export_rva = dword(directories)
    if not export_rva:
        return set()
    exports = offset(export_rva)
    names = offset(dword(exports + 32))
    result = set()
    for index in range(dword(exports + 24)):
        start = offset(dword(names + index * 4))
        end = data.index(b'\0', start)
        result.add(data[start:end].decode('ascii'))
    return result


def audit(api_dir, platform_dir, version, x86, rename):
    dll_files = {p.name.lower(): p for p in platform_dir.glob('*.dll')}
    windows = os.environ.get('WINDIR')
    if windows:
        system = Path(windows) / ('SysWOW64' if x86 else 'System32')
        for path in system.glob('*.dll'):
            dll_files.setdefault(path.name.lower(), path)
    cache = {}
    records = []
    guards = []
    for path in sorted(api_dir.glob('*.v')):
        libraries = []
        dll_block = False
        for number, line in active_lines(path, version_number(version), x86):
            if line.startswith('#err '):
                guards.append({'file': path.name, 'line': number, 'message': line[5:]})
            if line.startswith(':dlls'):
                libraries = re.findall(r'[\w.-]+\.dll', line, re.I)
                dll_block = True
            elif line.startswith(':'):
                dll_block = False
            elif dll_block and re.fullmatch(r'[\w.-]+\.dll', line, re.I):
                libraries.append(line)
            elif dll_block and '|' in line and not line.startswith('//'):
                # The first alias is x86 and the second alias is x64.
                aliases = line.split('//')[0].split('|')[1:]
                symbol = aliases[0 if x86 or len(aliases) == 1 else 1].strip()
                mapped = [rename.get(lib.lower(), lib.lower()) for lib in libraries]
                present = [lib for lib in mapped if lib in dll_files]
                for lib in present:
                    if lib not in cache:
                        cache[lib] = pe_exports(dll_files[lib])
                found = any(symbol in cache[lib] for lib in present)
                records.append({'file': path.name, 'line': number, 'libraries': libraries,
                                'mapped_libraries': mapped, 'symbol': symbol,
                                'status': 'found' if found else 'missing_symbol' if present else 'missing_library'})
    return {'version': version, 'architecture': 'x86' if x86 else 'x64',
            'scope': 'Named exports only; does not validate native ABI or vtables',
            'guards': guards, 'checked': len(records),
            'found': sum(r['status'] == 'found' for r in records),
            'failures': [r for r in records if r['status'] != 'found']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api', type=Path, required=True)
    parser.add_argument('--platform', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--x64', action='store_true')
    parser.add_argument('--map', action='append', default=[], metavar='OLD.DLL=NEW.DLL')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    mapping = dict(item.lower().split('=', 1) for item in args.map)
    result = audit(args.api, args.platform, args.version, not args.x64, mapping)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(output + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k != 'failures'}, ensure_ascii=False))
    if not args.out:
        print(output)
