"""BSL syntax-tree index, using 1c-syntax grammar and ANTLR 4.13.2.

Syntax recognition does not resolve platform types or execute preprocessor conditions.
"""
import re
from antlr4 import InputStream, CommonTokenStream, ParserRuleContext
from antlr4.error.ErrorListener import ErrorListener
from antlr4.atn.LexerATNSimulator import LexerATNSimulator
from antlr4.PredictionContext import PredictionContextCache
from antlr4.dfa.DFA import DFA
from antlr4.atn.PredictionMode import PredictionMode
from antlr4.error.ErrorStrategy import BailErrorStrategy
from antlr4.error.Errors import ParseCancellationException
from .bsl_grammar.BSLLexer import BSLLexer
from .bsl_grammar.BSLParser import BSLParser

VERSION = 'bsl-antlr-0.30.0-4.13.2-2'


class SparseEdges(dict):
    def __getitem__(self, key):
        return self.get(key)


class UnicodeLexerSimulator(LexerATNSimulator):
    # The Python runtime caches ASCII only. BSL identifiers and comments are
    # predominantly Cyrillic; sparse Unicode edges avoid repeated ATN walks
    # without allocating a million-entry array for each lexer state.
    MAX_DFA_EDGE = 0x10ffff

    def addDFAEdge(self, from_, tk, to=None, cfgs=None):
        if from_.edges is None:
            from_.edges = SparseEdges()
        return super().addDFAEdge(from_, tk, to, cfgs)


UNICODE_DFA = [DFA(state, i) for i, state in enumerate(BSLLexer.atn.decisionToState)]


class Diagnostics(ErrorListener):
    def __init__(self):
        self.items = []

    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):
        self.items.append({'line': line, 'column': column + 1, 'message': msg})


def walk(root):
    """Iterative traversal avoids additional recursion for deeply nested expressions."""
    pending = [root]
    while pending:
        node = pending.pop()
        if isinstance(node, ParserRuleContext):
            yield node
            pending.extend(reversed(node.children or []))


def source_of(source, ctx):
    return source[ctx.start.start:ctx.stop.stop + 1] if ctx and ctx.stop else ''


def real_terminal(terminal):
    return terminal is not None and terminal.symbol.tokenIndex >= 0


def method_symbol(source, ctx, diagnostics, lines, offsets):
    is_function = isinstance(ctx, BSLParser.FunctionContext)
    decl = ctx.funcDeclaration() if is_function else ctx.procDeclaration()
    ending = ctx.ENDFUNCTION_KEYWORD() if is_function else ctx.ENDPROCEDURE_KEYWORD()
    if not decl or not decl.subName() or not real_terminal(ending):
        return None
    if not real_terminal(decl.subName().IDENTIFIER()) or not real_terminal(decl.RPAREN()):
        return None
    keyword = decl.FUNCTION_KEYWORD() if is_function else decl.PROCEDURE_KEYWORD()
    declaration_line = (decl.ASYNC_KEYWORD() or keyword).symbol.line
    first = ctx.start.line
    comments = []
    # Only contiguous leading comments belong to the method, never region contents.
    while first > 1 and lines[first - 2].lstrip().startswith('//'):
        first -= 1
        comments.insert(0, lines[first - 1].strip()[2:].strip())
    directives = [source_of(source, n) for n in decl.children or []
                  if isinstance(n, (BSLParser.CompilerDirectiveContext, BSLParser.AnnotationContext))]
    # Also collect comments between compiler directives and the declaration.
    for line in lines[ctx.start.line - 1:declaration_line - 1]:
        if line.lstrip().startswith('//'):
            comments.append(line.strip()[2:].strip())
    parameters = []
    if decl.paramList():
        for param in decl.paramList().param():
            name = param.IDENTIFIER()
            if not real_terminal(name):
                continue
            parameters.append({'name': name.getText(), 'by_value': bool(param.VAL_KEYWORD()),
                               'default': source_of(source, param.defaultValue()) or None,
                               'line': name.symbol.line})
    calls, variables = [], []
    for node in walk(ctx.subCodeBlock()):
        if isinstance(node, BSLParser.SubVarDeclarationContext):
            variables.append(node.getText())
        if isinstance(node, BSLParser.AssignmentContext):
            left = node.lValue()
            if left and left.IDENTIFIER() and not left.acceptor():
                variables.append(left.IDENTIFIER().getText())
        if isinstance(node, (BSLParser.ForStatementContext, BSLParser.ForEachStatementContext)):
            variables.append(node.IDENTIFIER().getText())
        if not isinstance(node, (BSLParser.GlobalMethodCallContext, BSLParser.MethodCallContext)):
            continue
        name = node.methodName()
        if not name or not real_terminal(name.IDENTIFIER()):
            continue
        receiver = None
        if isinstance(node, BSLParser.MethodCallContext):
            access = node.parentCtx
            container = access.parentCtx
            if isinstance(container, BSLParser.ModifierContext):
                container = container.parentCtx
            receiver = re.sub(r'\s+', '', source[container.start.start:access.start.start])
        calls.append({'name': name.getText(), 'receiver': receiver, 'line': name.start.line,
                      'column': name.start.column + 1})
    end_line = ending.symbol.line
    method_errors = [w for w in diagnostics if ctx.start.line <= w['line'] <= end_line]
    start_offset = offsets[first - 1]
    return {'name': decl.subName().getText(), 'kind': 'function' if is_function else 'procedure',
            'parameters': source[decl.LPAREN().symbol.stop + 1:decl.RPAREN().symbol.start],
            'parameter_details': parameters, 'export': bool(decl.EXPORT_KEYWORD()),
            'async': bool(decl.ASYNC_KEYWORD()), 'directives': directives,
            'comment': '\n'.join(comments), 'start_line': first,
            'declaration_line': declaration_line, 'end_line': end_line,
            'code': source[start_offset:ending.symbol.stop + 1], 'calls': calls,
            'local_names': sorted(set(variables + [p['name'] for p in parameters])),
            'syntax_valid': not method_errors}


def extract(source, fast=True):
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    errors = Diagnostics()
    lexer = BSLLexer(InputStream(source))
    lexer._interp = UnicodeLexerSimulator(lexer, lexer.atn, UNICODE_DFA, PredictionContextCache())
    lexer.removeErrorListeners(); lexer.addErrorListener(errors)
    stream = CommonTokenStream(lexer)
    stream.fill()
    # Upstream puts unknown characters on the hidden channel; expose them as diagnostics.
    for token in stream.tokens:
        if token.type == BSLLexer.UNKNOWN:
            errors.items.append({'line': token.line, 'column': token.column + 1,
                                 'message': f'Неизвестный символ: {token.text}'})
    parser = BSLParser(stream)
    parser.removeErrorListeners()
    if fast:
        # Valid modules normally need no full-context prediction. On any SLL
        # failure restart with the original LL parser and diagnostic recovery.
        parser._interp.predictionMode = PredictionMode.SLL
        parser._errHandler = BailErrorStrategy()
        try:
            root = parser.file_()
        except ParseCancellationException:
            stream.seek(0)
            parser = BSLParser(stream)
            parser.removeErrorListeners(); parser.addErrorListener(errors)
            root = parser.file_()
    else:
        parser.addErrorListener(errors)
        root = parser.file_()
    methods = [ctx for ctx in walk(root) if isinstance(ctx, (BSLParser.ProcedureContext, BSLParser.FunctionContext))]
    symbols = [sym for ctx in methods if (sym := method_symbol(source, ctx, errors.items, lines, offsets))]
    module_names = [ctx.var_name().getText() for ctx in walk(root)
                    if isinstance(ctx, BSLParser.ModuleVarDeclarationContext)]
    for sym in symbols:
        sym['local_names'] = sorted(set(sym['local_names'] + module_names))
    return {'symbols': symbols, 'warnings': errors.items}


def search_text(value):
    expanded = re.sub(r'(?<=[a-zа-яё0-9])(?=[A-ZА-ЯЁ])', ' ', value)
    return value.casefold() + ' ' + expanded.casefold().replace('_', ' ')
