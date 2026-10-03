#!/usr/bin/env python3
"""TA Helper — fast interactive marks entry for Evaluation CSV sheets.

Usage:
    ta.py               scan the current directory for CSV sheets (or create one)
    ta.py <file.csv>    open (or create) that sheet directly
    ta.py <directory>   scan that directory

Inside, type `help` for the command cheat sheet.  Quick taste:
    quiz1                   select (or create) the "Quiz 1" column
    24F-0709 10             give 10 marks to 24F-0709 in the selected column
    0709 10  0712 8.5       roll suffixes work too, and several pairs per line
    quiz1 23F-0709 10       one-shot: column + roll + marks
    show / save / undo      pretty table, save with backup, undo last change
"""
import atexit
import csv
import curses
import datetime as dt
import io
import locale
import os
import re
import shlex
import shutil
import statistics
import sys
from pathlib import Path

try:
    import readline
except ImportError:  # e.g. Windows without pyreadline
    readline = None

FIXED = 4                      # Sr. | RollNo | Name | Total Marks — assessments start after these
BACKUP_DIR = '.ta_backups'
MAX_BACKUPS = 50               # per sheet
HISTORY_FILE = Path.home() / '.ta_helper_history'
ROLL_LOOSE = re.compile(r'(\d{2})([FLIKPM])-?(\d{4})', re.I)
DATE_TAIL = re.compile(r'^(.*?)\s*(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})\s*$')
MARK_RE = re.compile(r'(\d+(\.\d*)?|\.\d+)(\+(\d+(\.\d*)?|\.\d+))*')

# ───────────────────────────── colours & tables ─────────────────────────────

USE_COLOR = sys.stdout.isatty() and not os.environ.get('NO_COLOR')
ANSI = re.compile(r'\033\[[0-9;]*m')


def _style(code):
    if not USE_COLOR:
        return lambda s: str(s)
    return lambda s: f'\033[{code}m{s}\033[0m'


bold, dim, red, green, yellow, cyan, magenta = (_style(c) for c in ('1', '2', '31', '32', '33', '36', '35'))
hi = _style('1;36')          # selected column header
hl = _style('1;33')          # selected column values


def vlen(s):
    return len(ANSI.sub('', s))


def rl_safe(s):
    """Wrap ANSI escapes so readline computes the prompt width correctly."""
    return ANSI.sub(lambda m: f'\001{m.group(0)}\002', s)


def table(headers, rows, align, group=0):
    """Render a box-drawn table. headers: list of header rows; align: one of l/r/c per column."""
    ncol = len(headers[0])
    w = [max(vlen(r[i]) for r in headers + rows) for i in range(ncol)]

    def rule(l, m, r, ch='─'):
        return dim(l + m.join(ch * (x + 2) for x in w) + r)

    def fmt(cells, head=False):
        out = []
        for i, c in enumerate(cells):
            c = str(c)
            if head and '\033' not in c:
                c = bold(c)
            pad = w[i] - vlen(c)
            a = 'c' if head and align[i] != 'l' else align[i]
            if a == 'r':
                c = ' ' * pad + c
            elif a == 'c':
                c = ' ' * (pad // 2) + c + ' ' * (pad - pad // 2)
            else:
                c = c + ' ' * pad
            out.append(f' {c} ')
        bar = dim('│')
        return bar + bar.join(out) + bar

    lines = [rule('┌', '┬', '┐')]
    lines += [fmt(h, True) for h in headers]
    lines.append(rule('├', '┼', '┤'))
    for i, r in enumerate(rows):
        if group and i and i % group == 0:
            lines.append(rule('├', '┼', '┤', '┄'))
        lines.append(fmt(r))
    lines.append(rule('└', '┴', '┘'))
    return '\n'.join(lines)


def term_width():
    return shutil.get_terminal_size((120, 40)).columns


# ───────────────────────────── small helpers ─────────────────────────────

class CmdError(Exception):
    pass


class Quit(Exception):
    pass


def as_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def fmt_num(x):
    x = round(float(x), 2)
    return str(int(x)) if x == int(x) else f'{x:g}'


def key_of(name):
    return re.sub(r'[^a-z0-9]', '', name.lower())


def split_key(k):
    m = re.fullmatch(r'([a-z]*)(.*)', k)
    return m[1], m[2]


def pretty_name(tok):
    """quiz1 -> Quiz 1, mid -> Mid, a2 -> A2, lab_3 -> Lab 3"""
    m = re.fullmatch(r'([A-Za-z]+)[_-]?(\d*)', tok)
    if not m:
        return tok.replace('_', ' ')
    alpha, num = m.groups()
    if len(alpha) <= 2:
        return alpha.upper() + num
    return alpha.capitalize() + (f' {num}' if num else '')


def norm_roll(tok):
    m = ROLL_LOOSE.fullmatch(tok.strip())
    return f'{m[1]}{m[2].upper()}-{m[3]}' if m else None


def parse_date(s):
    """Accepts dd/mm/yyyy, d/m/yy, dd-mm-yyyy, dd.mm.yyyy, d/m (this year), today, yesterday."""
    s = s.strip().lower()
    today = dt.date.today()
    if s in ('today', 't', 'now'):
        return today.strftime('%d/%m/%Y')
    if s in ('yesterday', 'y'):
        return (today - dt.timedelta(days=1)).strftime('%d/%m/%Y')
    m = re.fullmatch(r'(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2}|\d{4}))?', s)
    if not m:
        return None
    y = today.year if m[3] is None else int(m[3]) + (2000 if len(m[3]) == 2 else 0)
    try:
        return dt.date(y, int(m[2]), int(m[1])).strftime('%d/%m/%Y')
    except ValueError:
        return None


def col_letter(i):
    s = ''
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def lock_path(p):
    return p.with_name(f'.~lock.{p.name}#')


def ask(prompt, default=''):
    """input() that doesn't pollute the command history."""
    try:
        s = input(rl_safe(prompt))
    except EOFError:
        print()
        s = ''
    if s and readline:
        n = readline.get_current_history_length()
        if n:
            readline.remove_history_item(n - 1)
    return s.strip() or default


def confirm(q, default=True):
    s = ask(f'{q} {dim("[Y/n]" if default else "[y/N]")} ').lower()
    return default if not s else s in ('y', 'yes')


# ───────────────────────────── CSV model ─────────────────────────────

class Assessment:
    def __init__(self, name, date='', clo='', weight='', total='', alt=''):
        self.name, self.date, self.clo, self.weight, self.total = name, date, clo, weight, total
        self.alt = alt                      # display label for columns with an empty header

    @property
    def label(self):
        return self.name or self.alt

    @property
    def key(self):
        return key_of(self.label)

    @property
    def header(self):                       # what goes in the "Eval Date" cell
        return f'{self.name} {self.date}'.strip()


class Student:
    def __init__(self, roll, name, extra='', marks=None):
        self.roll, self.name, self.extra = roll, name, extra
        self.marks = marks if marks is not None else []


class Sheet:
    def __init__(self):
        # first four cells of the four meta rows, kept verbatim so we never clobber anything
        self.meta = [['Instructor', '', '', 'CLO.No'],
                     ['', '', '', 'Eval Date'],
                     ['', 'Total Wgt', '', 'Weight'],
                     ['Sr.', 'RollNo', 'Name', 'Total Marks']]
        self.assessments = []
        self.students = []

    instructor = property(lambda s: s.meta[0][1], lambda s, v: s.meta[0].__setitem__(1, v))
    total_wgt = property(lambda s: s.meta[2][2], lambda s, v: s.meta[2].__setitem__(2, v))

    @classmethod
    def from_rows(cls, rows):
        rows = [list(r) for r in rows]
        hdr = next((i for i, r in enumerate(rows[:10])
                    if len(r) > 1 and key_of(r[1]) == 'rollno'), None)
        if hdr is None:
            raise ValueError('no "RollNo" header row found — is this an Evaluation sheet?')
        if hdr != 3:
            raise ValueError(f'expected the Sr./RollNo header on row 4, found it on row {hdr + 1}')

        def get(r, c):
            return rows[r][c].strip() if c < len(rows[r]) else ''

        sh = cls()
        sh.meta = [[get(i, c) for c in range(FIXED)] for i in range(4)]
        ncols = FIXED
        for r in rows:
            for c in range(len(r) - 1, FIXED - 1, -1):
                if r[c].strip():
                    ncols = max(ncols, c + 1)
                    break
        for c in range(FIXED, ncols):
            cell = get(1, c)
            m = DATE_TAIL.match(cell)
            name, date = (m[1], parse_date(m[2]) or m[2]) if m else (cell, '')
            sh.assessments.append(Assessment(name, date, get(0, c), get(2, c), get(3, c),
                                             alt=f'Col {col_letter(c)}'))
        for i in range(4, len(rows)):
            if not any(x.strip() for x in rows[i]):
                continue
            sh.students.append(Student(get(i, 1).upper(), get(i, 2), get(i, 3),
                                       [get(i, c) for c in range(FIXED, ncols)]))
        return sh

    def to_rows(self):
        n = len(self.assessments)
        rows = [list(m) + [''] * n for m in self.meta]
        for j, a in enumerate(self.assessments):
            c = FIXED + j
            rows[0][c], rows[1][c], rows[2][c], rows[3][c] = a.clo, a.header, a.weight, a.total
        for i, s in enumerate(self.students, 1):
            rows.append([str(i), s.roll, s.name, s.extra] + list(s.marks))
        return rows

    def find_roll(self, roll):
        return next((s for s in self.students if s.roll == roll), None)


def read_csv(path):
    raw = Path(path).read_bytes()
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = raw.decode('cp1252', errors='replace')
    newline = '\r\n' if '\r\n' in text else '\n'
    return list(csv.reader(io.StringIO(text, newline=''))), newline


def write_csv(path, rows, newline='\r\n'):
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'w', newline='', encoding='utf-8') as f:
        csv.writer(f, lineterminator=newline).writerows(rows)
    os.replace(tmp, path)


def describe(path):
    try:
        sh = Sheet.from_rows(read_csv(path)[0])
    except Exception:
        return None
    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime).strftime('%d/%m/%Y %H:%M')
    return f'{len(sh.students)} students · {len(sh.assessments)} assessments · {sh.instructor or "no instructor"} · {mtime}'


# ───────────────────────────── the app ─────────────────────────────

HELP = [
    ('Marks entry', [
        ('quiz1', 'select the "Quiz 1" column (offers to create it if missing)'),
        ('24F-0709 10', 'give 10 marks to 24F-0709 in the selected column'),
        ('0709 10', 'same — a unique roll suffix (3–4 digits) is enough'),
        ('0709 10 0712 8 …', 'several roll/marks pairs in one line'),
        ('quiz1 24F-0709 10', 'one-shot: column, roll, marks'),
        ('0709 3+4+2.5', 'marks can be a sum (handy for multi-part questions)'),
        ('0709 25!', 'force marks above the column total'),
        ('0709 -', 'clear that student\'s marks'),
        ('fill [quiz1] [all]', 'step through ungraded (or all) students one by one'),
    ]),
    ('Viewing & searching', [
        ('show  /  show all', 'pretty table (fits the screen, or every column)'),
        ('show quiz1 quiz2', 'only those columns'),
        ('24F-0709  /  0709', 'one student\'s full record'),
        ('find ali  /  ali', 'search students by name or roll'),
        ('missing [quiz1]', 'students without marks in a column'),
        ('stats [quiz1]', 'average / median / min / max / histogram'),
        ('info', 'all assessment columns with CLO, date, total, weight'),
        ('<Tab>', 'complete commands, columns, rolls — or type part of a name'),
    ]),
    ('Assessment columns', [
        ('new Quiz 1', 'create a column with an exact name (date = today)'),
        ('total [quiz1] 10', 'set total marks'),
        ('weight [quiz1] 5', 'set weight'),
        ('clo [quiz1] 2', 'set CLO no.'),
        ('date [quiz1] 3/10/2026', 'set eval date (dd/mm/yyyy, "today", "yesterday", or "-" to clear)'),
        ('set [quiz1] total=10 weight=5 clo=2 date=today', 'several at once'),
        ('rename [quiz1] Quiz One', 'rename a column'),
        ('drop quiz1', 'delete a column'),
    ]),
    ('Students & sheet', [
        ('student add 24F-0709 Ali Khan', 'add a student (kept in roll order); alias: add'),
        ('student rm 0709', 'remove a student'),
        ('student name 0709 New Name', 'rename a student'),
        ('student roll 0709 24F-0710', 'change a roll number'),
        ('instructor [Name]', 'show / set the instructor'),
        ('totalwgt [7]', 'show / set the sheet\'s Total Wgt cell'),
    ]),
    ('Files & history', [
        ('save [file.csv]', 'save (previous version goes to .ta_backups/)'),
        ('load [file.csv]', 'reload from disk, or open another sheet'),
        ('backups  /  restore 3', 'list backups / load one (then save to keep it)'),
        ('undo  /  redo', 'step through your changes'),
        ('quit', 'exit (asks to save unsaved changes); quit! skips the prompt'),
    ]),
]


class App:
    def __init__(self, path):
        self.path = Path(path)
        self.sheet = Sheet()
        self.cur = None
        self.saved = []
        self.newline = '\r\n'
        self.undo_stack, self.redo_stack = [], []
        self._skip_hist = False
        self._matches, self._sentinel, self._prompt = [], None, ''

        self.commands = {}
        for names, fn in [
            ('help h ?', self.cmd_help), ('show ls table view', self.cmd_show),
            ('info cols', self.cmd_info), ('find search', self.cmd_find),
            ('student st', self.cmd_student), ('add', lambda a: self.cmd_student(['add'] + a)),
            ('instructor', self.cmd_instructor), ('totalwgt', self.cmd_totalwgt),
            ('total', lambda a: self.cmd_field('total', a)),
            ('weight wgt', lambda a: self.cmd_field('weight', a)),
            ('clo', lambda a: self.cmd_field('clo', a)),
            ('date', lambda a: self.cmd_field('date', a)),
            ('set', self.cmd_set), ('rename', self.cmd_rename), ('new', self.cmd_new),
            ('use select sel', self.cmd_use), ('drop', self.cmd_drop),
            ('missing', self.cmd_missing), ('stats', self.cmd_stats), ('fill', self.cmd_fill),
            ('save', self.cmd_save), ('load open reload', self.cmd_load),
            ('backups', self.cmd_backups), ('restore', self.cmd_restore),
            ('undo', self.cmd_undo), ('redo', self.cmd_redo),
            ('clear cls', lambda a: print('\033[2J\033[H', end='')),
            ('quit exit q', self.cmd_quit), ('quit! q!', self.cmd_force_quit),
        ]:
            for n in names.split():
                self.commands[n] = fn
        self.open(self.path)

    # ── state ──
    @property
    def dirty(self):
        return self.sheet.to_rows() != self.saved

    def open(self, path):
        rows, nl = read_csv(path)
        self.sheet = Sheet.from_rows(rows)
        self.path, self.newline = Path(path), nl
        self.saved = self.sheet.to_rows()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.cur = self.sheet.assessments[-1] if self.sheet.assessments else None
        self.warn_lock()

    def set_rows(self, rows):
        key = self.cur.key if self.cur else None
        self.sheet = Sheet.from_rows(rows)
        self.cur = next((a for a in self.sheet.assessments if a.key == key), None)

    def warn_lock(self):
        if lock_path(self.path).exists():
            print(yellow('⚠  This file is open in LibreOffice/Excel (lock file present). '
                         'Close it there before saving, or it may overwrite your work.'))

    def col(self, a):
        return self.sheet.assessments.index(a)

    def graded(self, a):
        j = self.col(a)
        return sum(1 for s in self.sheet.students if s.marks[j].strip())

    # ── lookups ──
    def find_assess(self, tok):
        k = key_of(tok)
        if not k:
            return None
        A = self.sheet.assessments
        exact = [a for a in A if a.key == k]
        if exact:
            return exact[0]
        ta, tn = split_key(k)              # abbreviations: q1 -> quiz1, mid -> midterm
        if not ta:
            return None
        c = [a for a in A if split_key(a.key)[0].startswith(ta) and split_key(a.key)[1] == tn]
        return c[0] if len(c) == 1 else None

    def need_assess(self, tok):
        a = self.find_assess(tok)
        if not a:
            raise CmdError(f'No assessment column "{tok}". Columns: '
                           + (', '.join(x.key for x in self.sheet.assessments) or 'none yet'))
        return a

    def need_cur(self):
        if not self.cur:
            raise CmdError('No column selected — type its name first, e.g. quiz1')
        return self.cur

    def name_matches(self, tok):
        t = tok.lower()
        return [s for s in self.sheet.students if t in s.name.lower()]

    def try_roll(self, tok):
        r = norm_roll(tok)
        if r:
            return self.sheet.find_roll(r)
        if re.fullmatch(r'\d{3,4}', tok):
            c = [s for s in self.sheet.students if s.roll.endswith(tok)]
            if len(c) > 1:
                raise CmdError(f'"{tok}" is ambiguous: ' + ', '.join(s.roll for s in c))
            return c[0] if c else None
        return None

    def need_roll(self, tok):
        st = self.try_roll(tok)
        if st:
            return st
        if norm_roll(tok):
            raise CmdError(f'No student {norm_roll(tok)}. Add with: student add {norm_roll(tok)} <name>')
        if not re.search(r'\d', tok) and len(tok) >= 3:
            c = self.name_matches(tok)
            if len(c) == 1:
                return c[0]
            if len(c) > 1:
                raise CmdError(f'"{tok}" matches several students: '
                               + ', '.join(f'{s.roll} {s.name}' for s in c))
        raise CmdError(f'No student matching "{tok}"')

    # ── marks ──
    def parse_mark(self, m, a):
        force = m.endswith('!')
        m = m.rstrip('!')
        if m in ('-', 'x', 'clear', 'del'):
            return ''
        if not MARK_RE.fullmatch(m):
            raise CmdError(f'"{m}" is not a valid mark (use a number, a sum like 3+4, or - to clear)')
        v = sum(float(p) for p in m.split('+'))
        tot = as_float(a.total)
        if tot is not None and v > tot and not force:
            raise CmdError(f'{fmt_num(v)} exceeds {a.label} total of {a.total} (append ! to force)')
        return fmt_num(v)

    def enter_marks(self, a, toks):
        if len(toks) % 2:
            raise CmdError('Expected <roll> <marks> pairs, e.g. 24F-0709 10')
        j = self.col(a)
        pairs = [(self.need_roll(r), self.parse_mark(m, a)) for r, m in zip(toks[::2], toks[1::2])]
        for st, val in pairs:              # validate everything first, then apply
            old = st.marks[j]
            st.marks[j] = val
            tot = f'/{a.total}' if a.total else ''
            was = dim(f'  (was {old})') if old and old != val else ''
            if val:
                print(f'{green("✓")} {a.label} · {bold(st.roll)} {st.name} → {hl(val)}{dim(tot)}{was}')
            else:
                print(f'{yellow("✗")} {a.label} · {bold(st.roll)} {st.name} → cleared{was}')
        left = len(self.sheet.students) - self.graded(a)
        if left and len(pairs) and self.graded(a) > 0:
            print(dim(f'  {left} left in {a.label}'))
        elif not left:
            print(green(f'  🎉 all {len(self.sheet.students)} students graded in {a.label}'))

    # ── dispatch ──
    def dispatch(self, line):
        toks = line.split()
        head = toks[0].lower()
        if head in self.commands:
            return self.commands[head](toks[1:])

        st = self.try_roll(toks[0])
        if st:
            if len(toks) == 1:
                return self.show_student(st)
            return self.enter_marks(self.need_cur(), toks)

        a = self.find_assess(toks[0])
        if a:
            self.cur = a
            if len(toks) == 1:
                return self.print_summary(a, '→ ')
            return self.enter_marks(a, toks[1:])

        if norm_roll(toks[0]):
            self.need_roll(toks[0])        # raises a helpful error

        word = toks[0]
        if not re.search(r'\d', word) and len(word) >= 2:
            matches = self.name_matches(word)
            if len(toks) >= 2 and len(toks) % 2 == 0 and len(matches) == 1:
                return self.enter_marks(self.need_cur(), toks)
            if len(toks) == 1 and matches:
                if len(matches) == 1:
                    return self.show_student(matches[0])
                self.print_grid(matches, None)
                print(dim(f'  (to create a column called "{pretty_name(word)}" instead: new {word})'))
                return

        if re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', word) and len(toks) % 2 == 1:
            name = pretty_name(word)
            if confirm(f'No column "{word}". Create {bold(name)}?'):
                a = self.create_assess(name)
                if len(toks) > 1:
                    self.enter_marks(a, toks[1:])
            return
        raise CmdError(f'Don\'t understand "{line}" — type help')

    # ── views ──
    def print_summary(self, a, prefix=''):
        bits = [hi(a.label)]
        bits.append(f'total {a.total}' if a.total else yellow('no total'))
        if a.weight:
            bits.append(f'weight {a.weight}')
        if a.clo:
            bits.append(f'CLO {a.clo}')
        if a.date:
            bits.append(a.date)
        bits.append(f'{self.graded(a)}/{len(self.sheet.students)} graded')
        print(prefix + dim(' · ').join(bits))

    def weighted(self, st):
        tot, any_ = 0.0, False
        for j, a in enumerate(self.sheet.assessments):
            t, w, m = as_float(a.total), as_float(a.weight), as_float(st.marks[j])
            if t and w is not None and m is not None:
                tot += m / t * w
                any_ = True
        return fmt_num(tot) if any_ else ''

    def has_weights(self):
        return any(as_float(a.total) and as_float(a.weight) is not None for a in self.sheet.assessments)

    def mark_cell(self, a, val, selected):
        if not val:
            return dim('·')
        t, v = as_float(a.total), as_float(val)
        if t is not None and v is not None and v > t:
            return red(val)
        return hl(val) if selected else val

    def print_grid(self, students, idx=None, fit=True):
        A = self.sheet.assessments
        idx = list(range(len(A))) if idx is None else idx
        cur_i = A.index(self.cur) if self.cur in A else None
        wtd = self.has_weights()

        def sub(a):
            return ' '.join(x for x in (f'/{a.total}' if a.total else '', f'w{a.weight}' if a.weight else '') if x)

        def colw(j):
            return max([len(A[j].label), len(sub(A[j])), 1] + [len(s.marks[j]) for s in students]) + 3

        def name_of(s):
            return s.name if len(s.name) <= 24 else s.name[:23] + '…'

        hidden = 0
        if fit and idx:
            base = 1 + (max([3] + [len(str(len(self.sheet.students)))]) + 3) + 11 \
                   + max([4] + [len(name_of(s)) for s in students]) + 3 + (8 if wtd else 0)
            pref = (lambda j: abs(j - cur_i)) if cur_i is not None else (lambda j: len(A) - j)
            used, chosen = base, []
            for j in sorted(idx, key=pref):
                if used + colw(j) <= term_width():
                    chosen.append(j)
                    used += colw(j)
            hidden = len(idx) - len(chosen)
            idx = sorted(chosen)

        sr = {id(s): i for i, s in enumerate(self.sheet.students, 1)}
        h1 = ['#', 'Roll No', 'Name'] + [hi(A[j].label) if j == cur_i else A[j].label for j in idx]
        h2 = ['', '', ''] + [dim(sub(A[j])) for j in idx]
        align = 'rll' + 'r' * len(idx)
        if wtd:
            sw = sum(as_float(a.weight) or 0 for a in A if as_float(a.total))
            h1.append('Wtd')
            h2.append(dim(f'/{fmt_num(sw)}'))
            align += 'r'
        rows = []
        for s in students:
            r = [dim(sr[id(s)]), bold(s.roll), name_of(s)]
            r += [self.mark_cell(A[j], s.marks[j], j == cur_i) for j in idx]
            if wtd:
                r.append(self.weighted(s) or dim('·'))
            rows.append(r)
        heads = [h1, h2] if any(vlen(c) for c in h2) else [h1]
        if not rows:
            print(dim('  (no students)'))
            return
        print(table(heads, rows, align, group=5))
        foot = [f'{len(students)} student{"s" * (len(students) != 1)}']
        if self.cur:
            j = cur_i
            foot.append(f'{sum(1 for s in students if s.marks[j])}/{len(students)} graded in {self.cur.label}')
        if hidden:
            foot.append(yellow(f'{hidden} column(s) hidden — use "show all" or "show <name>"'))
        print(dim('  ' + ' · '.join(foot)))

    def show_student(self, st):
        A = self.sheet.assessments
        sr = self.sheet.students.index(st) + 1
        print(f'{bold(st.roll)}  {st.name}  {dim(f"(Sr. {sr})")}')
        if not A:
            print(dim('  no assessment columns yet'))
            return
        rows = []
        for j, a in enumerate(A):
            sel = a is self.cur
            t, w, m = as_float(a.total), as_float(a.weight), as_float(st.marks[j])
            contrib = fmt_num(m / t * w) if t and w is not None and m is not None else ''
            rows.append([hi(a.label) if sel else a.label, a.date, a.clo,
                         self.mark_cell(a, st.marks[j], sel), a.total, a.weight, contrib])
        wsum = self.weighted(st)
        if wsum:
            rows.append([bold('Weighted total'), '', '', '', '', '', bold(wsum)])
        print(table([['Assessment', 'Date', 'CLO', 'Marks', 'Total', 'Weight', 'Wtd']], rows, 'llcrrrr'))

    # ── commands: viewing ──
    def cmd_help(self, args):
        for title, items in HELP:
            print(bold(cyan(title)))
            for cmd, desc in items:
                print(f'  {yellow(cmd)}{" " * max(2, 36 - len(cmd))}{desc}')
        print(dim('Roll numbers look like 24F-0709 (campus letter F/L/I/K/P/M). '
                  'Column names ignore case & spaces: quiz1 = "Quiz 1", q1 works too.'))

    def cmd_show(self, args):
        A = self.sheet.assessments
        if args and args[0].lower() == 'all':
            return self.print_grid(self.sheet.students, None, fit=False)
        if args:
            return self.print_grid(self.sheet.students, [A.index(self.need_assess(t)) for t in args], fit=False)
        self.print_grid(self.sheet.students)

    def cmd_info(self, args):
        sh = self.sheet
        print(f'{bold("File")}        {self.path.name}{yellow(" (unsaved changes)") if self.dirty else ""}')
        print(f'{bold("Instructor")}  {sh.instructor or dim("—")}')
        print(f'{bold("Total Wgt")}   {sh.total_wgt or dim("—")}')
        print(f'{bold("Students")}    {len(sh.students)}')
        if not sh.assessments:
            print(dim('No assessment columns yet — type e.g. quiz1 to create one.'))
            return
        rows = []
        for i, a in enumerate(sh.assessments, 1):
            sel = a is self.cur
            rows.append([dim(i), hi(a.label) if sel else a.label, dim(a.key), a.date or dim('—'),
                         a.clo or dim('—'), a.total or yellow('?'), a.weight or dim('—'),
                         f'{self.graded(a)}/{len(sh.students)}'])
        print(table([['#', 'Assessment', 'Key', 'Eval Date', 'CLO', 'Total', 'Weight', 'Graded']],
                    rows, 'rlllcrrr'))
        sw = sum(as_float(a.weight) or 0 for a in sh.assessments)
        tw = as_float(sh.total_wgt)
        if tw is not None and abs(sw - tw) > 1e-9:
            print(yellow(f'  note: column weights add up to {fmt_num(sw)}, sheet Total Wgt says {sh.total_wgt}'))

    def cmd_find(self, args):
        if not args:
            raise CmdError('usage: find <part of name or roll>')
        q = ' '.join(args).lower()
        hits = [s for s in self.sheet.students if q in s.name.lower() or q in s.roll.lower()]
        if not hits:
            raise CmdError(f'No student matches "{q}"')
        self.print_grid(hits)

    def cmd_missing(self, args):
        a = self.need_assess(args[0]) if args else self.need_cur()
        j = self.col(a)
        miss = [s for s in self.sheet.students if not s.marks[j].strip()]
        if not miss:
            print(green(f'Everyone has marks in {a.label} ✓'))
            return
        sr = {id(s): i for i, s in enumerate(self.sheet.students, 1)}
        print(table([['#', 'Roll No', 'Name']], [[dim(sr[id(s)]), bold(s.roll), s.name] for s in miss], 'rll', 5))
        print(dim(f'  {len(miss)} of {len(self.sheet.students)} missing in {a.label}'))

    def cmd_stats(self, args):
        a = self.need_assess(args[0]) if args else self.need_cur()
        j = self.col(a)
        vals = [v for v in (as_float(s.marks[j]) for s in self.sheet.students) if v is not None]
        self.print_summary(a)
        if not vals:
            print(dim('  no marks yet'))
            return
        t = as_float(a.total)
        pct = (lambda v: f' {dim(f"({v / t * 100:.0f}%)")}') if t else (lambda v: '')
        rows = [['Mean', fmt_num(statistics.mean(vals)) + pct(statistics.mean(vals))],
                ['Median', fmt_num(statistics.median(vals)) + pct(statistics.median(vals))],
                ['Highest', fmt_num(max(vals)) + pct(max(vals))],
                ['Lowest', fmt_num(min(vals)) + pct(min(vals))]]
        if len(vals) > 1:
            rows.append(['Std dev', fmt_num(statistics.stdev(vals))])
        print(table([['Stat', 'Value']], rows, 'lr'))
        top = t or max(vals) or 1
        bins = [0] * 5
        for v in vals:
            bins[min(4, int(v / top * 5))] += 1
        peak = max(bins) or 1
        for b in range(5):
            lo, hi_ = top * b / 5, top * (b + 1) / 5
            bar = '█' * round(bins[b] / peak * 30)
            print(f'  {fmt_num(lo):>5}–{fmt_num(hi_):<5} {cyan(bar)} {dim(bins[b])}')

    # ── commands: assessments ──
    def create_assess(self, name):
        if self.find_assess(name) and self.find_assess(name).key == key_of(name):
            raise CmdError(f'Column "{name}" already exists')
        a = Assessment(name, date=dt.date.today().strftime('%d/%m/%Y'))
        self.sheet.assessments.append(a)
        for s in self.sheet.students:
            s.marks.append('')
        self.cur = a
        print(green(f'＋ Created {bold(name)} dated {a.date}') +
              dim('  — set it up with e.g.  set total=10 weight=5 clo=1'))
        return a

    def cmd_new(self, args):
        if not args:
            raise CmdError('usage: new <column name>, e.g. new Quiz 1')
        name = pretty_name(args[0]) if len(args) == 1 else ' '.join(args)
        self.create_assess(name)

    def cmd_use(self, args):
        if not args:
            raise CmdError('usage: use <column>')
        self.cur = self.need_assess(args[0])
        self.print_summary(self.cur, '→ ')

    def cmd_drop(self, args):
        a = self.need_assess(args[0]) if args else self.need_cur()
        n = self.graded(a)
        if not confirm(red(f'Delete column {a.label}') + (f' and its {n} marks' if n else '') + '?', False):
            return
        j = self.col(a)
        del self.sheet.assessments[j]
        for s in self.sheet.students:
            del s.marks[j]
        if self.cur is a:
            self.cur = None
        print(yellow(f'Deleted {a.label}') + dim('  (undo brings it back)'))

    def target_and_rest(self, args):
        """[assess] value…  →  (assessment, value tokens)."""
        if len(args) >= 2 and self.find_assess(args[0]):
            return self.find_assess(args[0]), args[1:]
        return self.need_cur(), args

    def set_field(self, a, field, val):
        if val.lower() in ('-', 'none', 'clear'):
            new = ''
        elif field in ('total', 'weight'):
            v = as_float(val)
            if v is None or v < 0:
                raise CmdError(f'{field} must be a non-negative number, got "{val}"')
            new = fmt_num(v)
        elif field == 'date':
            new = parse_date(val)
            if not new:
                raise CmdError(f'Bad date "{val}" — use dd/mm/yyyy, d/m, today or yesterday')
        else:
            new = val
        old = getattr(a, field)
        setattr(a, field, new)
        print(f'{green("✓")} {a.label} {field}: {dim(old or "—")} → {bold(new or "—")}')
        if field == 'total' and new:
            j = self.col(a)
            over = [s for s in self.sheet.students if (as_float(s.marks[j]) or 0) > float(new)]
            if over:
                print(yellow(f'  ⚠ {len(over)} student(s) now above the total: ' +
                             ', '.join(f'{s.roll} ({s.marks[j]})' for s in over)))

    def cmd_field(self, field, args):
        if not args:
            a = self.need_cur()
            print(f'{a.label} {field}: {getattr(a, field) or dim("—")}')
            return
        a, rest = self.target_and_rest(args)
        self.set_field(a, field, ' '.join(rest))

    def cmd_set(self, args):
        try:
            args = shlex.split(' '.join(args))
        except ValueError as e:
            raise CmdError(str(e))
        a = self.need_cur()
        if args and '=' not in args[0]:
            a = self.need_assess(args[0])
            args = args[1:]
        if not args:
            raise CmdError('usage: set [column] total=10 weight=5 clo=1 date=today name="Quiz 1"')
        pairs = []
        for kv in args:
            k, sep, v = kv.partition('=')
            k = k.lower()
            if not sep or k not in ('total', 'weight', 'wgt', 'clo', 'date', 'name'):
                raise CmdError(f'Bad setting "{kv}" — keys: total weight clo date name')
            pairs.append(('weight' if k == 'wgt' else k, v))
        for k, v in pairs:
            if k == 'name':
                self.do_rename(a, v)
            else:
                self.set_field(a, k, v)

    def do_rename(self, a, name):
        name = name.strip()
        if not name:
            raise CmdError('New name is empty')
        other = next((x for x in self.sheet.assessments if x.key == key_of(name) and x is not a), None)
        if other:
            raise CmdError(f'Another column is already called {other.label}')
        old = a.label
        a.name = name
        print(f'{green("✓")} renamed {old} → {bold(name)}')

    def cmd_rename(self, args):
        if not args:
            raise CmdError('usage: rename [column] <new name>')
        a, rest = self.target_and_rest(args)
        self.do_rename(a, ' '.join(rest))

    # ── commands: students & sheet ──
    def cmd_student(self, args):
        if not args:
            return self.print_grid(self.sheet.students)
        sub = args[0].lower()
        S = self.sheet.students
        if sub == 'add':
            if len(args) < 3:
                raise CmdError('usage: student add <roll> <full name>')
            roll = norm_roll(args[1])
            if not roll:
                raise CmdError(f'"{args[1]}" is not a roll number like 24F-0709')
            if self.sheet.find_roll(roll):
                raise CmdError(f'{roll} already exists')
            st = Student(roll, ' '.join(args[2:]), '', [''] * len(self.sheet.assessments))
            pos = next((i for i, s in enumerate(S) if s.roll > roll), len(S))
            S.insert(pos, st)
            print(f'{green("＋")} added {bold(roll)} {st.name} as Sr. {pos + 1}')
        elif sub in ('rm', 'del', 'remove', 'delete'):
            if len(args) != 2:
                raise CmdError('usage: student rm <roll>')
            st = self.need_roll(args[1])
            n = sum(1 for m in st.marks if m)
            if confirm(red(f'Remove {st.roll} {st.name}') + (f' (has {n} marks)' if n else '') + '?', False):
                S.remove(st)
                print(yellow(f'Removed {st.roll}') + dim('  (undo brings them back)'))
        elif sub == 'name':
            if len(args) < 3:
                raise CmdError('usage: student name <roll> <new name>')
            st = self.need_roll(args[1])
            old, st.name = st.name, ' '.join(args[2:])
            print(f'{green("✓")} {st.roll}: {dim(old)} → {bold(st.name)}')
        elif sub == 'roll':
            if len(args) != 3:
                raise CmdError('usage: student roll <roll> <new roll>')
            st = self.need_roll(args[1])
            new = norm_roll(args[2])
            if not new:
                raise CmdError(f'"{args[2]}" is not a roll number like 24F-0709')
            if self.sheet.find_roll(new):
                raise CmdError(f'{new} already exists')
            old, st.roll = st.roll, new
            print(f'{green("✓")} {st.name}: {dim(old)} → {bold(new)}')
        else:
            self.show_student(self.need_roll(args[0]))

    def cmd_instructor(self, args):
        if not args:
            print(f'Instructor: {self.sheet.instructor or dim("—")}')
            return
        old, self.sheet.instructor = self.sheet.instructor, ' '.join(args)
        print(f'{green("✓")} instructor: {dim(old or "—")} → {bold(self.sheet.instructor)}')

    def cmd_totalwgt(self, args):
        if not args:
            print(f'Total Wgt: {self.sheet.total_wgt or dim("—")}')
            return
        v = args[0]
        if v != '-' and as_float(v) is None:
            raise CmdError('Total Wgt must be a number (or - to clear)')
        old = self.sheet.total_wgt
        self.sheet.total_wgt = '' if v == '-' else fmt_num(v)
        print(f'{green("✓")} Total Wgt: {dim(old or "—")} → {bold(self.sheet.total_wgt or "—")}')

    def cmd_fill(self, args):
        all_ = any(x.lower() == 'all' for x in args)
        rest = [x for x in args if x.lower() != 'all']
        a = self.need_assess(rest[0]) if rest else self.need_cur()
        self.cur = a
        j = self.col(a)
        todo = [s for s in self.sheet.students if all_ or not s.marks[j].strip()]
        if not todo:
            print(green(f'Nothing to fill — everyone has marks in {a.label}'))
            return
        print(f'Filling {hi(a.label)}{dim("/" + a.total if a.total else "")}: {len(todo)} students. '
              + dim('Enter = skip · b = back · q = stop · - = clear'))
        i = 0
        while i < len(todo):
            s = todo[i]
            cur = f' [{s.marks[j]}]' if s.marks[j] else ''
            try:
                v = ask(f'  {dim(f"{i + 1}/{len(todo)}")} {bold(s.roll)} {s.name[:24]:<24}{dim(cur)} › ')
            except KeyboardInterrupt:
                print()
                break
            if v.lower() == 'q':
                break
            if v.lower() == 'b':
                i = max(0, i - 1)
                continue
            if not v:
                i += 1
                continue
            try:
                s.marks[j] = self.parse_mark(v, a)
            except CmdError as e:
                print(red(f'    ✗ {e}'))
                continue
            i += 1
        print(dim(f'  {self.graded(a)}/{len(self.sheet.students)} graded in {a.label}'))

    # ── commands: files & history ──
    def backups(self, path=None):
        path = path or self.path
        d = path.parent / BACKUP_DIR
        return sorted(d.glob(f'{path.stem}__*.csv'), reverse=True) if d.is_dir() else []

    def make_backup(self, path):
        d = path.parent / BACKUP_DIR
        d.mkdir(exist_ok=True)
        b = d / f'{path.stem}__{dt.datetime.now():%Y%m%d-%H%M%S}.csv'
        shutil.copy2(path, b)
        for old in self.backups(path)[MAX_BACKUPS:]:
            old.unlink(missing_ok=True)
        return b

    def save(self, target):
        rows = self.sheet.to_rows()
        if target == self.path and target.exists() and rows == self.saved:
            print(dim('No changes to save.'))
            return
        if lock_path(target).exists():
            print(yellow('⚠  LibreOffice/Excel has this file open — reload it there after saving, '
                         'and don\'t save from there or it will overwrite these marks.'))
        b = self.make_backup(target) if target.exists() else None
        write_csv(target, rows, self.newline)
        self.path, self.saved = target, rows
        print(green(f'✓ Saved {target.name}') + (dim(f'  (backup: {BACKUP_DIR}/{b.name})') if b else ''))

    def cmd_save(self, args):
        target = self.path
        if args:
            target = Path(os.path.expanduser(' '.join(args)))
            if target.suffix.lower() != '.csv':
                target = target.with_name(target.name + '.csv')
            if not target.is_absolute():
                target = self.path.parent / target
        self.save(target)

    def cmd_load(self, args):
        path = self.path
        if args:
            path = Path(os.path.expanduser(' '.join(args)))
            if not path.is_absolute():
                path = self.path.parent / path
            if not path.exists():
                raise CmdError(f'{path} does not exist')
        if self.dirty and not confirm(yellow('Discard unsaved changes?'), False):
            return
        try:
            Sheet.from_rows(read_csv(path)[0])
        except Exception as e:
            raise CmdError(f'Cannot load {path.name}: {e}')
        self.open(path)
        self._skip_hist = True
        self.banner(short=True)

    def cmd_backups(self, args):
        bs = self.backups()
        if not bs:
            print(dim(f'No backups yet for {self.path.name} (one is made each time you save).'))
            return
        rows = []
        for i, b in enumerate(bs, 1):
            ts = dt.datetime.strptime(b.stem.rsplit('__', 1)[1], '%Y%m%d-%H%M%S')
            info = describe(b) or 'unreadable'
            rows.append([dim(i), ts.strftime('%d/%m/%Y %H:%M:%S'), info.rsplit(' · ', 1)[0]])
        print(table([['#', 'Saved at', 'Contents']], rows, 'rll'))
        print(dim('  restore <#> loads one into the editor (then save to keep it)'))

    def cmd_restore(self, args):
        bs = self.backups()
        if not args or not args[0].isdigit() or not 1 <= int(args[0]) <= len(bs):
            raise CmdError(f'usage: restore <#> with # from "backups" (1–{len(bs)})')
        b = bs[int(args[0]) - 1]
        self.set_rows(read_csv(b)[0])
        print(green(f'✓ Restored {b.name}') + dim(' — not saved yet: "save" to keep it, "undo" to go back'))

    def cmd_undo(self, args):
        if not self.undo_stack:
            raise CmdError('Nothing to undo')
        self.redo_stack.append(self.sheet.to_rows())
        self.set_rows(self.undo_stack.pop())
        self._skip_hist = True
        print(f'↶ undone {dim(f"({len(self.undo_stack)} more)")}')

    def cmd_redo(self, args):
        if not self.redo_stack:
            raise CmdError('Nothing to redo')
        self.undo_stack.append(self.sheet.to_rows())
        self.set_rows(self.redo_stack.pop())
        self._skip_hist = True
        print('↷ redone')

    def cmd_quit(self, args):
        raise Quit

    def cmd_force_quit(self, args):
        sys.exit(0)

    def maybe_save_before_quit(self):
        if not self.dirty:
            return True
        s = ask(yellow('Unsaved changes. Save before quitting? ') + dim('[Y/n/c=cancel] ')).lower()
        if s in ('c', 'cancel'):
            return False
        if s in ('', 'y', 'yes'):
            self.save(self.path)
        return True

    # ── readline completion ──
    def setup_readline(self):
        if not readline:
            return
        readline.set_completer_delims(' \t\n')
        readline.set_completer(self.complete)
        if 'libedit' in (readline.__doc__ or ''):
            readline.parse_and_bind('bind ^I rl_complete')
        else:
            readline.parse_and_bind('tab: complete')
            readline.parse_and_bind('set show-all-if-ambiguous on')
            readline.parse_and_bind('set completion-ignore-case on')
            readline.set_completion_display_matches_hook(self.display_matches)
        try:
            readline.read_history_file(HISTORY_FILE)
        except OSError:
            pass
        readline.set_history_length(2000)
        atexit.register(lambda: readline.write_history_file(HISTORY_FILE))

    def complete(self, text, state):
        if state == 0:
            try:
                self._matches = self.candidates(text)
            except Exception:
                self._matches = []
        return self._matches[state] if state < len(self._matches) else None

    def candidates(self, text):
        self._sentinel = None
        words = readline.get_line_buffer()[:readline.get_begidx()].split()
        rolls = [s.roll for s in self.sheet.students]
        keys = [a.key for a in self.sheet.assessments]
        cmds = sorted(c for c in self.commands if c.isalpha() and len(c) > 2)
        w0 = words[0].lower() if words else ''
        takes_assess = ('total', 'weight', 'wgt', 'clo', 'date', 'set', 'rename', 'use', 'select', 'sel',
                        'drop', 'missing', 'stats', 'fill', 'show', 'ls', 'table', 'view')
        if not words:
            pool = keys + cmds + rolls
        elif w0 in ('student', 'st'):
            pool = (['add', 'rm', 'name', 'roll'] + rolls) if len(words) == 1 else (rolls if len(words) == 2 else [])
        elif w0 in ('help', 'h'):
            pool = cmds
        elif w0 in ('save', 'load', 'open'):
            pool = sorted(p.name for p in self.path.parent.glob('*.csv'))
        elif w0 in takes_assess:
            pool = keys + (['all'] if w0 in ('fill', 'show', 'ls', 'table', 'view') else [])
        elif self.find_assess(w0) and w0 not in self.commands:
            pool = rolls if len(words) % 2 == 1 else []
        elif self.sheet.find_roll(norm_roll(w0) or '') or re.fullmatch(r'\d{3,4}', w0):
            pool = rolls if len(words) % 2 == 0 else []
        else:
            pool = []
        t = text.lower()
        pre = [p for p in pool if p.lower().startswith(t)]
        if pre or not text or not set(rolls) & set(pool):
            return pre
        # fuzzy: part of a roll or a name → roll numbers
        fuzzy = [s.roll for s in self.sheet.students if t in s.roll.lower() or t in s.name.lower()]
        if len(fuzzy) > 1:
            self._sentinel = text            # keeps what you typed instead of a useless common prefix
            fuzzy = [text] + fuzzy
        return fuzzy

    def display_matches(self, substitution, matches, longest):
        items = []
        for m in matches:
            if m == self._sentinel:
                continue
            st = self.sheet.find_roll(m)
            items.append(f'{m} {dim(st.name[:20])}' if st else m)
        if not items:
            return
        w = max(vlen(i) for i in items) + 3
        per = max(1, term_width() // w)
        print()
        for i in range(0, len(items), per):
            print(''.join(it + ' ' * (w - vlen(it)) for it in items[i:i + per]))
        print(self._prompt + readline.get_line_buffer(), end='', flush=True)

    # ── main loop ──
    def prompt(self):
        a = self.cur
        p = cyan(self.path.stem[:28]) + (yellow('*') if self.dirty else '')
        if a:
            p += ' ' + hi(f'[{a.label}' + (f' /{a.total}' if a.total else '') + ']')
        return p + ' › '

    def banner(self, short=False):
        sh = self.sheet
        print(f'{dim("╭─")} {bold("TA Helper")} {dim("─")} {cyan(self.path.name)}')
        print(f'{dim("│")}  Instructor: {sh.instructor or dim("—")} · {len(sh.students)} students · '
              f'{len(sh.assessments)} assessment column(s)')
        if self.cur:
            print(dim('│  ') + 'Selected: ', end='')
            self.print_summary(self.cur)
        if not short:
            print(dim('╰─ "help" for commands · Tab completes rolls, names & columns · e.g. "quiz1" then "0709 10"'))

    def run(self):
        self.setup_readline()
        self.banner()
        while True:
            self._prompt = self.prompt()
            try:
                line = input(rl_safe(self._prompt)).strip()
            except EOFError:
                print()
                if self.maybe_save_before_quit():
                    break
                continue
            except KeyboardInterrupt:
                print()
                continue
            if not line:
                continue
            before = self.sheet.to_rows()
            self._skip_hist = False
            try:
                self.dispatch(line)
            except Quit:
                if self.maybe_save_before_quit():
                    break
            except CmdError as e:
                print(red('✗ ') + str(e))
            except KeyboardInterrupt:
                print(dim(' (cancelled)'))
            if not self._skip_hist and self.sheet.to_rows() != before:
                self.undo_stack.append(before)
                del self.undo_stack[:-200]
                self.redo_stack.clear()


# ───────────────────────────── startup: pick / create a sheet ─────────────────────────────

def _curses_pick(scr, title, options):
    curses.curs_set(0)
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    i, top = 0, 0
    while True:
        scr.erase()
        h, w = scr.getmaxyx()
        scr.addnstr(0, 1, title, w - 2, curses.A_BOLD)
        scr.addnstr(1, 1, '↑/↓ or j/k move · Enter select · 1–9 jump · q quit', w - 2, curses.A_DIM)
        rows = max(1, h - 4)
        top = min(max(top, i - rows + 1), i)
        for k in range(top, min(len(options), top + rows)):
            label, sub = options[k]
            y = 3 + k - top
            sel = k == i
            text = f' {"›" if sel else " "} {label} '
            scr.addnstr(y, 1, text, w - 2, (curses.A_REVERSE | curses.A_BOLD) if sel else curses.A_BOLD)
            if sub and len(text) + 3 < w:
                scr.addnstr(y, len(text) + 3, sub, w - len(text) - 4, curses.A_DIM)
        ch = scr.getch()
        if ch in (curses.KEY_UP, ord('k')):
            i = (i - 1) % len(options)
        elif ch in (curses.KEY_DOWN, ord('j'), 9):
            i = (i + 1) % len(options)
        elif ch in (10, 13, curses.KEY_ENTER, ord(' ')):
            return i
        elif ch in (ord('q'), 27):
            return None
        elif ord('1') <= ch <= ord('9') and ch - ord('1') < len(options):
            return ch - ord('1')


def pick(title, options):
    """options: list of (label, dim subtitle). Returns index or None."""
    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            return curses.wrapper(_curses_pick, title, options)
        except curses.error:
            pass
    print(bold(title))
    for k, (label, sub) in enumerate(options, 1):
        print(f'  {cyan(k)}. {label}  {dim(sub)}')
    s = ask('Choose a number (Enter to quit): ')
    return int(s) - 1 if s.isdigit() and 1 <= int(s) <= len(options) else None


def scan(directory):
    found = []
    for p in sorted(directory.glob('*.csv'), key=lambda p: -p.stat().st_mtime):
        d = describe(p)
        if d:
            found.append((p, d))
    return found


def create_new(directory, filename=None):
    print(bold('Create a new evaluation sheet'))
    name = filename or ask(f'File name {dim("[Evaluation.csv]")}: ', 'Evaluation.csv')
    p = Path(os.path.expanduser(name))
    p = p if p.is_absolute() else directory / p
    if p.suffix.lower() != '.csv':
        p = p.with_name(p.name + '.csv')
    if p.exists() and not confirm(yellow(f'{p.name} exists. Overwrite it?'), False):
        return None
    sheet = Sheet()
    sheet.instructor = ask('Instructor name: ')
    sheet.total_wgt = ask(f'Total Wgt {dim("(optional)")}: ')
    sources = [(f, d) for f, d in scan(directory) if f != p]
    if sources:
        k = pick('Copy the student list from…', [(f.name, d) for f, d in sources] + [('Start with no students', '')])
        if k is not None and k < len(sources):
            src = Sheet.from_rows(read_csv(sources[k][0])[0])
            sheet.students = [Student(s.roll, s.name) for s in src.students]
    write_csv(p, sheet.to_rows())
    print(green(f'✓ Created {p.name} with {len(sheet.students)} students'))
    return p


def choose(directory):
    found = scan(directory)
    if not found:
        print(yellow(f'No evaluation CSVs found in {directory}.'))
        return create_new(directory) if confirm('Create a new one?') else None
    opts = [(p.name, d) for p, d in found] + [('＋ Create a new sheet', ''), ('Quit', '')]
    k = pick(f'Found {len(found)} evaluation sheet(s) in {directory}', opts)
    if k is None or k == len(found) + 1:
        return None
    if k == len(found):
        return create_new(directory)
    return found[k][0]


def main():
    locale.setlocale(locale.LC_ALL, '')
    args = sys.argv[1:]
    if args and args[0] in ('-h', '--help'):
        print(__doc__)
        return
    target = Path(os.path.expanduser(args[0])) if args else Path.cwd()
    try:
        if target.is_dir():
            path = choose(target.resolve())
        elif target.exists():
            path = target
        else:
            path = create_new(target.parent.resolve(), target.name) if confirm(
                f'{target} does not exist. Create it?') else None
        if path is None:
            return
        app = App(path)
    except KeyboardInterrupt:
        print()
        return
    except (ValueError, OSError) as e:
        print(red(f'✗ Could not open sheet: {e}'))
        sys.exit(1)
    app.run()


if __name__ == '__main__':
    main()
