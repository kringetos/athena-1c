"""Line-addressable evidence shared by tools, transport and durable memory."""


def read_window(content, start_line=1, line_count=100, start_column=0, max_chars=5000):
    lines = content.splitlines(keepends=True)
    if start_column and (start_line>len(lines) or start_column>=len(lines[start_line-1])):
        raise ValueError('start_column должен находиться внутри указанной строки.')
    selected = lines[start_line-1:start_line-1+line_count]
    text = ''.join(selected)[start_column:]
    return clip_window({'content':text, 'start_line':start_line, 'start_column':start_column,
                        'total_lines':len(lines), 'truncated':False,
                        'next_line':start_line+len(selected) if start_line+len(selected)<=len(lines) else None,
                        'next_column':0}, max_chars)


def clip_window(window, max_chars=5000, field='content'):
    result = dict(window)
    text = result.get(field, '')
    delivered = text[:max_chars]
    # Prefer complete lines, but make progress even on generated single-line code.
    if len(text)>max_chars and '\n' in delivered:
        delivered = delivered[:delivered.rfind('\n')+1]
    start = result.get('start_line', 1)
    column = result.get('start_column', 0)
    result[field] = delivered
    result['end_line'] = start + len(delivered.splitlines())-1 if delivered else start-1
    result['end_column'] = (len(delivered.rsplit('\n',1)[-1]) + (column if '\n' not in delivered else 0))
    if len(delivered)<len(text):
        result.update(truncated=True, next_line=start+delivered.count('\n'),
                      next_column=result['end_column'])
    else:
        result.setdefault('next_column', 0)
    result['partial'] = bool(result.get('truncated') or result.get('next_line') or start>1 or column)
    return result


def key_fragments(text, start_line=1, limit=1800, start_column=0):
    """Preserve actual head, middle and tail evidence with separate locations."""
    if len(text)<=limit:
        return [clip_window({'content':text,'start_line':start_line,'start_column':start_column},limit)]
    fragments=[]
    size=limit//3
    for offset in sorted({0, len(text)//2, max(0,len(text)-size)}):
        prefix=text[:offset]
        line=start_line+prefix.count('\n')
        column=len(prefix.rsplit('\n',1)[-1])+(start_column if '\n' not in prefix else 0)
        fragments.append(clip_window({'content':text[offset:offset+size],'start_line':line,
                                     'start_column':column,'truncated':True},size))
    return fragments
