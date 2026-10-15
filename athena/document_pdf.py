"""Local Markdown-to-PDF rendering. No HTML execution, external fonts or images."""
from io import BytesIO
from pathlib import Path
from threading import RLock
from xml.sax.saxutils import escape, quoteattr
import re

import mistune
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle, HRFlowable

_LOCK = RLock()
_FONTS = Path(__file__).parent / 'assets' / 'fonts'


def _fonts():
    if 'AthenaSans' in pdfmetrics.getRegisteredFontNames():
        return
    for name, filename in [('AthenaSans', 'Ubuntu-R.ttf'), ('AthenaBold', 'Ubuntu-B.ttf'),
                           ('AthenaItalic', 'Ubuntu-RI.ttf'), ('AthenaBoldItalic', 'Ubuntu-BI.ttf'),
                           ('AthenaMono', 'UbuntuMono-R.ttf')]:
        pdfmetrics.registerFont(TTFont(name, str(_FONTS / filename)))
    pdfmetrics.registerFontFamily('AthenaSans', normal='AthenaSans', bold='AthenaBold',
                                 italic='AthenaItalic', boldItalic='AthenaBoldItalic')
    pdfmetrics.registerFontFamily('AthenaMono', normal='AthenaMono', bold='AthenaMono',
                                 italic='AthenaMono', boldItalic='AthenaMono')


def _inline(tokens):
    result = []
    for item in tokens:
        kind = item['type']
        text = _inline(item.get('children', []))
        if kind in {'text', 'inline_html'}:
            result.append(escape(item.get('raw', '')))
        elif kind == 'codespan':
            result.append('<font name="AthenaMono">' + escape(item.get('raw', '')) + '</font>')
        elif kind in {'strong', 'emphasis'}:
            tag = 'b' if kind == 'strong' else 'i'
            result.append(f'<{tag}>{text}</{tag}>')
        elif kind in {'softbreak', 'linebreak'}:
            result.append('<br/>' if kind == 'linebreak' else ' ')
        elif kind == 'link':
            url = item.get('attrs', {}).get('url', '')
            if re.match(r'^(https?://|mailto:)', url, re.I):
                result.append('<link href=' + quoteattr(url) + ' color="#5B4DB1">' + text + '</link>')
            else:
                result.append(text)
        elif kind == 'image':
            # Never fetch URLs or open paths supplied in generated Markdown.
            result.append('[Изображение: ' + (text or 'без описания') + ']')
        else:
            result.append(text or escape(item.get('raw', '')))
    return ''.join(result)


def render_pdf(job, content):
    """Export the editor's current text without changing the saved document."""
    # ReportLab's font registry is global; serialize initialization and rendering.
    with _LOCK:
        _fonts()
        return _render(job, content)


def _render(job, content):
    output = BytesIO()
    width = A4[0] - 112  # Account for the page frame's six-point inner padding.
    ink = colors.HexColor('#202735')
    body = ParagraphStyle('Body', fontName='AthenaSans', fontSize=10, leading=15,
                          textColor=ink, spaceAfter=8, splitLongWords=True)
    small = ParagraphStyle('Small', parent=body, fontSize=8, leading=11, textColor=colors.HexColor('#536078'))
    code = ParagraphStyle('Code', parent=body, fontName='AthenaMono', fontSize=8.5, leading=12,
                          backColor=colors.HexColor('#F2F4F8'), borderPadding=7, spaceBefore=5, spaceAfter=9)
    cell = ParagraphStyle('Cell', parent=body, fontSize=8.5, leading=12, spaceAfter=0)
    story = []
    def paragraph(text, style=body):
        return Paragraph(text or '&#160;', style)
    def blocks(tokens, indent=0):
        for token in tokens:
            kind = token['type']
            if kind == 'heading':
                level = token['attrs']['level']
                style = ParagraphStyle('Heading', parent=body, fontName='AthenaBold',
                    fontSize={1:22, 2:15, 3:12}.get(level, 10.5),
                    leading={1:28, 2:21, 3:17}.get(level, 15),
                    spaceBefore=16 if level > 1 else 8, spaceAfter=10, keepWithNext=True)
                story.append(paragraph(_inline(token['children']), style))
            elif kind in {'paragraph', 'block_text'}:
                style = ParagraphStyle('Indented', parent=body, leftIndent=indent)
                story.append(paragraph(_inline(token.get('children', [])), style))
            elif kind == 'block_code':
                # Break by measured glyph widths; even a single huge line remains within A4.
                lines = []
                for line in token.get('raw', '').expandtabs(4).splitlines() or ['']:
                    parts, current, used = [], '', 0
                    for char in line:
                        size = pdfmetrics.stringWidth(char, 'AthenaMono', code.fontSize)
                        if used + size > width - indent - 24 and current:
                            parts.append(current); current = ''; used = 0
                        current += char; used += size
                    parts.append(current)
                    for part in parts:
                        lines.append(escape(part).replace(' ', '&#160;') or '&#160;')
                story.append(paragraph('<br/>'.join(lines), ParagraphStyle('CodeBlock', parent=code,
                    leftIndent=indent + 7, rightIndent=7)))
            elif kind == 'list':
                start = token.get('attrs', {}).get('start', 1)
                ordered = token.get('attrs', {}).get('ordered', False)
                for index, item in enumerate(token['children']):
                    children = list(item.get('children', []))
                    prefix = f'{start + index}.' if ordered else '•'
                    if children and children[0]['type'] in {'block_text', 'paragraph'}:
                        style = ParagraphStyle('List', parent=body, leftIndent=indent + 15, firstLineIndent=-12)
                        story.append(paragraph(escape(prefix) + ' ' + _inline(children.pop(0).get('children', [])), style))
                    blocks(children, indent + 15)
            elif kind == 'table':
                rows = []
                for section in token['children']:
                    table_rows = [section] if section['type'] == 'table_head' else section['children']
                    for row in table_rows:
                        rows.append([paragraph(_inline(c.get('children', [])), cell) for c in row['children']])
                if rows:
                    columns = max(map(len, rows))
                    for row in rows:
                        row.extend([paragraph('', cell)] * (columns - len(row)))
                    table = LongTable(rows, colWidths=[(width-indent)/columns]*columns,
                                      repeatRows=1, hAlign='LEFT', splitInRow=1)
                    table.setStyle(TableStyle([
                        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#EAE7F7')),
                        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#F6F7FA')]),
                        ('VALIGN', (0,0), (-1,-1), 'TOP'),
                        ('LINEBELOW', (0,0), (-1,0), .6, colors.HexColor('#9B8DD0')),
                        ('LEFTPADDING', (0,0), (-1,-1), 7), ('RIGHTPADDING', (0,0), (-1,-1), 7),
                        ('TOPPADDING', (0,0), (-1,-1), 7), ('BOTTOMPADDING', (0,0), (-1,-1), 7)]))
                    story.extend([table, Spacer(1, 10)])
            elif kind == 'block_quote':
                blocks(token.get('children', []), indent + 15)
            elif kind == 'thematic_break':
                story.append(HRFlowable(width='100%', color=colors.HexColor('#D8DDE6'), spaceBefore=8, spaceAfter=12))
            elif kind == 'block_html':
                story.append(paragraph(escape(token.get('raw', ''))))
            elif kind != 'blank_line':
                if token.get('children'): blocks(token['children'], indent)
                elif token.get('raw'): story.append(paragraph(escape(token['raw'])))
    ast = mistune.create_markdown(renderer='ast', plugins=['table'])
    if not content.lstrip().startswith('# '):
        blocks(ast('# ' + job['title']))
    blocks(ast(content))
    blocks(ast('---\n\n## Данные генерации Athena'))
    story.append(paragraph('Черновик документации. Требует проверки разработчиком.', small))
    for warning in job.get('warnings', []): story.append(paragraph(escape(warning), small))
    if job.get('model'): story.append(paragraph('Модель: ' + escape(job['model']), small))
    for source in job.get('sources', []):
        label = f"{source['path']} · строки {source['start_line']}-{source['end_line']}"
        if source.get('draft'): label += ' · несохранённый черновик'
        story.append(paragraph(escape(label), small))
        story.append(paragraph('SHA-256: ' + escape(source.get('hash', '')), small))
    def decorate(canvas, doc):
        canvas.saveState()
        canvas.setFont('AthenaSans', 8)
        canvas.setFillColor(colors.HexColor('#758096'))
        canvas.drawString(50, A4[1]-28, 'ATHENA / ДОКУМЕНТАЦИЯ')
        canvas.drawRightString(A4[0]-50, 27, str(doc.page))
        canvas.restoreState()
    document = SimpleDocTemplate(output, pagesize=A4, rightMargin=50, leftMargin=50,
        topMargin=48, bottomMargin=45, title=job['title'], author='Athena',
        allowSplitting=True)
    document.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return output.getvalue()
