#!/usr/bin/env python3
"""Generate a structural flowchart for a WDL workflow, from the WDL text.

    wdl_flowchart.py <workflow.wdl>... [--svg] [--png] [--check] [-o DIR]
    wdl_flowchart.py --all <dir> [--svg]

Writes <DIR>/<workflow>.flow.dot, and with --svg/--png renders it via graphviz. `--check`
regenerates in memory and exits non-zero if the committed .dot differs, so a pre-commit
hook can stop a diagram going stale.

Nodes are calls, clusters are scatter/if blocks, edges are data dependencies. See README.md
for what the picture does and does not mean - the limitations are not incidental, and
reading a graph as more than it claims is the main way to be misled by one.
"""
import argparse
import os
import re
import subprocess
import sys

__version__ = "1.0.0"

# ---------------------------------------------------------------------------- lexing

IMPORT = re.compile(r'^\s*import\s+"([^"]+)"(?:\s+as\s+(\w+))?')
TOKEN = re.compile(
    r'^\s*(?:'
    r'(?P<workflow>workflow\s+(?P<wfname>\w+)\s*\{)'
    r'|(?P<task>task\s+(?P<taskname>\w+)\s*\{)'
    r'|(?P<scatter>scatter\s*\((?P<scexpr>.*?)\)\s*\{)'
    r'|(?P<ifblock>if\s*\((?P<ifexpr>.*)\)\s*\{)'
    r'|(?P<call>call\s+(?P<callee>[\w.]+)(?:\s+as\s+(?P<alias>\w+))?)'
    r')'
)
# A declaration: a type, a name, '='. Types include user structs, so any capitalised word
# qualifies; that is deliberately loose, and the cost is a stray entry, not a wrong edge.
DECL = re.compile(r'^\s*(?:Array|Map|Pair|File|String|Int|Float|Boolean|Object|[A-Z]\w*)'
                  r'[\w\[\]\?\+, ]*\s+(\w+)\s*=\s*(.*)$')
BARE_DECL = re.compile(r'^\s*(?:Array|Map|Pair|File|String|Int|Float|Boolean|Object|[A-Z]\w*)'
                       r'[\w\[\]\?\+, ]*\s+(\w+)\s*$')
REF = re.compile(r'\b(\w+)\.\w+')


def strip_comments(line):
    """Drop a trailing # comment, respecting quotes."""
    out, q = [], None
    for ch in line:
        if q:
            out.append(ch)
            if ch == q:
                q = None
        elif ch in '"\'':
            q = ch
            out.append(ch)
        elif ch == '#':
            break
        else:
            out.append(ch)
    return ''.join(out)


def lex(text):
    """Comments removed, command bodies removed, continuation lines joined.

    Command bodies must go FIRST: they hold arbitrary shell, so a line starting `call `
    or an unbalanced bracket inside one would otherwise derail both the tokeniser and the
    continuation joiner.
    """
    raw = text.split('\n')
    kept, i = [], 0
    while i < len(raw):
        line = raw[i]
        if re.match(r'^\s*command\s*<<<', line):
            kept.append(re.sub(r'command\s*<<<.*', 'command <<< STRIPPED >>>', line))
            i += 1
            while i < len(raw) and '>>>' not in raw[i]:
                i += 1
            i += 1
            continue
        if re.match(r'^\s*command\s*\{', line):
            depth = line.count('{') - line.count('}')
            kept.append(re.sub(r'command\s*\{.*', 'command { STRIPPED }', line))
            i += 1
            while i < len(raw) and depth > 0:
                depth += raw[i].count('{') - raw[i].count('}')
                i += 1
            continue
        kept.append(strip_comments(line))
        i += 1

    # join continuations: a line whose brackets are unbalanced continues onto the next
    joined, buf, bal = [], '', 0
    for line in kept:
        if not line.strip():
            if not buf:
                joined.append(line)
            continue
        piece = line if not buf else buf + ' ' + line.strip()
        bal = (piece.count('(') - piece.count(')')
               + piece.count('[') - piece.count(']'))
        if bal > 0:
            buf = piece
        else:
            joined.append(piece)
            buf = ''
    if buf:
        joined.append(buf)
    return joined


# ---------------------------------------------------------------------------- model

class Node:
    def __init__(self, alias, callee, imported):
        self.alias, self.callee, self.imported = alias, callee, imported
        self.deps = set()


class Block:
    def __init__(self, kind, label):
        self.kind, self.label = kind, label
        self.children, self.calls = [], []


class Workflow:
    def __init__(self):
        self.name = None
        self.root = Block('root', '')
        self.nodes = {}
        self.decls = {}
        self.inputs, self.outputs, self.imports = [], [], []
        # Names that look like dependencies but are not calls: scatter variables and import
        # aliases. Without these the "unresolved" count is all noise and tells you nothing.
        self.scatter_vars, self.unresolved = set(), set()


def parse(path):
    wf = Workflow()
    lines = lex(open(path).read())

    depth, stack, cur = 0, [], wf.root
    in_task, section = None, None
    pending, pending_depth = None, None

    for line in lines:
        if not line.strip():
            continue

        imp = IMPORT.match(line)
        if imp:
            alias = imp.group(2) or os.path.basename(imp.group(1))
            wf.imports.append(alias)

        m = TOKEN.match(line)
        if m:
            if m.group('workflow'):
                wf.name = m.group('wfname')
            elif m.group('task'):
                in_task = m.group('taskname')
            elif not in_task and m.group('scatter'):
                sc = m.group('scexpr').strip()
                v = re.match(r'(\w+)\s+in\s', sc)
                if v:
                    wf.scatter_vars.add(v.group(1))
                b = Block('scatter', sc)
                cur.children.append(b)
                stack.append((depth, cur))
                cur = b
            elif not in_task and m.group('ifblock'):
                b = Block('if', m.group('ifexpr').strip())
                cur.children.append(b)
                stack.append((depth, cur))
                cur = b
            elif not in_task and m.group('call'):
                callee = m.group('callee')
                alias = m.group('alias') or callee.split('.')[-1]
                n = Node(alias, callee, '.' in callee)
                cur.calls.append(n)
                wf.nodes[alias] = n
                pending, pending_depth = n, depth

        if not in_task:
            st = line.strip()
            if st.startswith('input {'):
                section = 'input'
            elif st.startswith('output {'):
                section = 'output'
            elif section and st == '}':
                section = None
            elif section == 'input':
                d = DECL.match(line) or BARE_DECL.match(line)
                if d:
                    wf.inputs.append(d.group(1))
            elif section == 'output':
                d = DECL.match(line)
                if d:
                    wf.outputs.append(d.group(1))
            else:
                d = DECL.match(line)
                if d:
                    wf.decls[d.group(1)] = d.group(2)

        if pending is not None:
            for ref in REF.findall(line):
                pending.deps.add(ref)
            for w in re.findall(r'\b(\w+)\b', line):
                if w in wf.decls:
                    pending.deps.add(w)

        closes = line.count('}')
        depth += line.count('{') - closes
        if pending is not None and closes and depth <= pending_depth:
            pending = None
        if in_task and depth == 0:
            in_task = None
        while stack and depth <= stack[-1][0]:
            _, cur = stack.pop()

    return wf


def resolve(wf):
    """Map dependency names onto call aliases, following declarations transitively.

    Without this a workflow that threads outputs through intermediate declarations
    (`File x = select_first([a.bam])` then `input: b = x`) would show almost no edges.
    """
    def calls_in(expr, seen):
        found = {r for r in REF.findall(expr) if r in wf.nodes}
        for w in re.findall(r'\b(\w+)\b', expr):
            if w in wf.decls and w not in seen:
                found |= calls_in(wf.decls[w], seen | {w})
        return found

    for n in wf.nodes.values():
        real = set()
        for d in n.deps:
            if d in wf.nodes:
                real.add(d)
            elif d in wf.decls:
                real |= calls_in(wf.decls[d], {d})
            elif (d in wf.inputs or d in wf.scatter_vars or d in wf.imports
                  or '.' in d):
                pass
            else:
                wf.unresolved.add(d)
        n.deps = real - {n.alias}


# ---------------------------------------------------------------------------- emitting

HEADER = '''// Generated by wdl_flowchart.py {ver} from {src}. DO NOT EDIT.
// Regenerate: wdl_flowchart.py {src} --svg
//
// Calls are boxes, scatter/if are clusters, edges are data dependencies. This is a
// DECLARATION-level view: see the tool's README for what it cannot show.
digraph {name} {{
  rankdir=TB;
  compound=true;
  bgcolor="white";
  nodesep=0.35;
  ranksep=0.5;
  node [fontname="Helvetica" fontsize=10 style="filled,rounded" shape=box
        fillcolor="#eef4fb" color="#7ba7d7"];
  edge [fontname="Helvetica" fontsize=9 color="#555555"];
  graph [fontname="Helvetica" fontsize=10 labeljust=l];
'''


def esc(s, limit=64):
    s = re.sub(r'\s+', ' ', s.replace('\\', ' ').replace('"', "'")).strip()
    return s if len(s) <= limit else s[:limit - 3] + '...'


def note_box(name, title, items, limit=12):
    shown = items[:limit]
    label = "\\n".join(shown) + ("\\n..." if len(items) > limit else "")
    return [f'  subgraph cluster_{name} {{',
            f'    label="{title}"; labeljust=c;',
            '    style="filled,rounded"; fillcolor="#f7f7f7"; color="#cccccc";',
            '    node [shape=note fillcolor="#ffffff" color="#888888"];',
            f'    {name.upper()} [label="{label}"]',
            '  }']


def emit(wf, src):
    out = [HEADER.format(ver=__version__, src=os.path.basename(src),
                         name=wf.name or 'workflow')]
    ctr = [0]

    def walk(b, indent='  '):
        for n in b.calls:
            label = n.alias if n.alias == n.callee else f"{n.alias}\\n({n.callee})"
            extra = (' color="#9a6fb0" style="filled,rounded,diagonals"'
                     if n.imported else '')
            out.append(f'{indent}{n.alias} [label="{label}"{extra}]')
        for child in b.children:
            ctr[0] += 1
            fill = ('fillcolor="#fdf3e7" color="#e0a35c" style="filled,rounded"'
                    if child.kind == 'scatter' else
                    'fillcolor="#f6f6f6" color="#999999" style="filled,rounded,dashed"')
            out.append(f'{indent}subgraph cluster_{ctr[0]} {{')
            out.append(f'{indent}  label="{child.kind} ({esc(child.label)})"; {fill};')
            walk(child, indent + '  ')
            out.append(f'{indent}}}')

    if wf.inputs:
        out += note_box('inputs', 'workflow inputs', wf.inputs)
    walk(wf.root)
    if wf.outputs:
        out += note_box('outputs', 'workflow outputs', wf.outputs)

    out.append('')
    for n in sorted(wf.nodes.values(), key=lambda x: x.alias):
        for d in sorted(n.deps):
            out.append(f'  {d} -> {n.alias}')

    roots = sorted(n.alias for n in wf.nodes.values() if not n.deps)
    leaves = sorted(n.alias for n in wf.nodes.values()
                    if not any(n.alias in m.deps for m in wf.nodes.values()))
    for r in roots:
        if wf.inputs:
            out.append(f'  INPUTS -> {r} [style=dashed color="#bbbbbb"]')
    for lf in leaves:
        if wf.outputs:
            out.append(f'  {lf} -> OUTPUTS [style=dashed color="#bbbbbb"]')

    if wf.imported_any():
        out.append('')
        out.append('  legend_imported [shape=box style="filled,rounded,diagonals" '
                   'fillcolor="#eef4fb" color="#9a6fb0" '
                   'label="hatched = call into an imported file;\\nits internals are not shown"]')

    out.append('}')
    return '\n'.join(out) + '\n'


Workflow.imported_any = lambda self: any(n.imported for n in self.nodes.values())


# ---------------------------------------------------------------------------- driver

def render(dot_path, wf_name, outdir, fmt, extra):
    if not any(os.access(os.path.join(p, 'dot'), os.X_OK)
               for p in os.environ.get('PATH', '').split(os.pathsep)):
        print("graphviz 'dot' not on PATH; wrote .dot only", file=sys.stderr)
        return None
    target = os.path.join(outdir, f'{wf_name}.flow.{fmt}')
    subprocess.run(['dot', f'-T{fmt}'] + extra + [dot_path, '-o', target], check=True)
    return target


def process(path, args):
    wf = parse(path)
    if not wf.name:
        print(f"{path}: no workflow block, skipped", file=sys.stderr)
        return True
    resolve(wf)
    dot = emit(wf, path)

    base = os.path.dirname(os.path.abspath(path))
    outdir = args.outdir or (os.path.join(base, 'docs')
                             if os.path.isdir(os.path.join(base, 'docs')) else base)
    dot_path = os.path.join(outdir, f'{wf.name}.flow.dot')

    if args.check:
        if not os.path.exists(dot_path):
            print(f"MISSING {dot_path} (run without --check to create it)", file=sys.stderr)
            return False
        if open(dot_path).read() != dot:
            print(f"STALE {dot_path} does not match {path}", file=sys.stderr)
            return False
        print(f"ok {dot_path}")
        return True

    os.makedirs(outdir, exist_ok=True)
    with open(dot_path, 'w') as fh:
        fh.write(dot)
    edges = sum(len(n.deps) for n in wf.nodes.values())
    extra = f", {len(wf.unresolved)} unresolved refs" if wf.unresolved else ""
    print(f"{dot_path}  ({len(wf.nodes)} calls, {edges} edges{extra})")
    if args.verbose and wf.unresolved:
        print(f"    unresolved: {' '.join(sorted(wf.unresolved))}", file=sys.stderr)
    for want, fmt, ex in ((args.svg, 'svg', []), (args.png, 'png', ['-Gdpi=150'])):
        if want:
            t = render(dot_path, wf.name, outdir, fmt, ex)
            if t:
                print(f"{t}")
    return True


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('wdl', nargs='*')
    ap.add_argument('--all', metavar='DIR',
                    help='process every .wdl found under DIR')
    ap.add_argument('-o', '--outdir',
                    help='output dir (default: a docs/ beside the WDL if it exists, '
                         'else beside the WDL)')
    ap.add_argument('--svg', action='store_true')
    ap.add_argument('--png', action='store_true')
    ap.add_argument('--check', action='store_true',
                    help='exit 1 if a committed .dot is stale; writes nothing')
    ap.add_argument('-v', '--verbose', action='store_true',
                    help='list dependency names that could not be resolved')
    ap.add_argument('--version', action='version', version=__version__)
    args = ap.parse_args()

    targets = list(args.wdl)
    if args.all:
        for root, _, files in os.walk(args.all):
            targets += [os.path.join(root, f) for f in sorted(files)
                        if f.endswith('.wdl')]
    if not targets:
        ap.error('give at least one .wdl, or --all DIR')

    ok = True
    for t in targets:
        try:
            ok = process(t, args) and ok
        except Exception as exc:                                  # noqa: BLE001
            print(f"{t}: {type(exc).__name__}: {exc}", file=sys.stderr)
            ok = False
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
