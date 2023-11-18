from enum import Enum
import clingo
import clingo.script
import signal
import argparse
import os
import logging
from itertools import permutations
from collections import defaultdict
from time import perf_counter
from contextlib import contextmanager
from .core import Literal
from math import comb
import threading

clingo.script.enable_python()

TIMEOUT=600
EVAL_TIMEOUT=0.001
MAX_LITERALS=40
MAX_SOLUTIONS=1
CLINGO_ARGS=''
MAX_RULES=2
MAX_VARS=6
MAX_BODY=6
MAX_EXAMPLES=10000
BATCH_SIZE=20000
ANYTIME_TIMEOUT=10


# class syntax
class Constraint(Enum):
    GENERALISATION = 1
    SPECIALISATION = 2
    UNSAT = 3
    REDUNDANCY_CONSTRAINT1 = 4
    REDUNDANCY_CONSTRAINT2 = 5
    TMP_ANDY = 6
    BANISH = 7


def parse_args():
    parser = argparse.ArgumentParser(description='Popper is an ILP system based on learning from failures')

    parser.add_argument('kbpath', help='Path to files to learn from')
    parser.add_argument('--noisy', default=False, action='store_true', help='tell Popper that there is noise')
    parser.add_argument('--bkcons', default=False, action='store_true', help='deduce background constraints from Datalog background (EXPERIMENTAL!)')
    parser.add_argument('--timeout', type=float, default=TIMEOUT, help=f'Overall timeout in seconds (default: {TIMEOUT})')
    parser.add_argument('--max-literals', type=int, default=MAX_LITERALS, help=f'Maximum number of literals allowed in program (default: {MAX_LITERALS})')
    parser.add_argument('--max-body', type=int, default=MAX_BODY, help=f'Maximum number of body literals allowed in rule (default: {MAX_BODY})')
    parser.add_argument('--max-vars', type=int, default=MAX_VARS, help=f'Maximum number of variables allowed in rule (default: {MAX_VARS})')
    parser.add_argument('--max-rules', type=int, default=MAX_RULES, help=f'Maximum number of rules allowed in a recursive program (default: {MAX_RULES})')
    parser.add_argument('--eval-timeout', type=float, default=EVAL_TIMEOUT, help=f'Prolog evaluation timeout in seconds (default: {EVAL_TIMEOUT})')
    parser.add_argument('--stats', default=False, action='store_true', help='Print statistics at end of execution')
    parser.add_argument('--quiet', '-q', default=False, action='store_true', help='Hide information during learning')
    parser.add_argument('--debug', default=False, action='store_true', help='Print debugging information to stderr')
    parser.add_argument('--showcons', default=False, action='store_true', help='Show constraints deduced during the search')
    parser.add_argument('--solver', default='rc2', choices=['clingo', 'rc2', 'uwr', 'wmaxcdcl'], help='Select a solver for the combine stage (default: rc2)')
    parser.add_argument('--anytime-solver', default=None, choices=['wmaxcdcl', 'nuwls'], help='Select an anytime MaxSAT solver (default: None)')
    parser.add_argument('--anytime-timeout', type=int, default=ANYTIME_TIMEOUT, help=f'Maximum timeout (seconds) for each anytime MaxSAT call (default: {ANYTIME_TIMEOUT})')
    parser.add_argument('--batch-size', type=int, default=BATCH_SIZE, help=f'Combine batch size (default: {BATCH_SIZE})')
    parser.add_argument('--functional-test', default=False, action='store_true', help='Run functional test')
    parser.add_argument('--datalog', default=False, action='store_true', help='EXPERIMENTAL FEATURE: use recall to order literals in rules')
    parser.add_argument('--no-bias', default=False, action='store_true', help='EXPERIMENTAL FEATURE: do not use language bias')
    parser.add_argument('--order-space', default=False, action='store_true', help='EXPERIMENTAL FEATURE: search space ordered by size')


    return parser.parse_args()

def timeout(settings, func, args=(), kwargs={}, timeout_duration=1):
    result = None
    class TimeoutError(Exception):
        pass

    def handler(signum, frame):
        raise TimeoutError()
    
    if not (hasattr(signal, 'SIGALRM') and hasattr(signal, 'alarm')):
        return _windows_timeout(settings, func, args, kwargs, timeout_duration)

    # set the timeout handler
    signal.signal(signal.SIGALRM, handler)
    signal.alarm(timeout_duration)
    try:
        result = func(*args, **kwargs)
    except TimeoutError as _exc:
        settings.logger.warn(f'TIMEOUT OF {int(settings.timeout)} SECONDS EXCEEDED')
        return result
    except AttributeError as moo:
        if '_SolveEventHandler' in str(moo):
            settings.logger.warn(f'TIMEOUT OF {int(settings.timeout)} SECONDS EXCEEDED')
            return result
        raise moo
    finally:
        signal.alarm(0)

    return result

def _windows_timeout(settings, func, args=(), kwargs={}, timeout_duration=1):
    """
    A replacement for the `timeout` function that works on Windows, since the `signal` module is not fully supported on Windows.
    """
    result = None

    def target():
        nonlocal result
        try:
            result = func(*args, **kwargs)
        except Exception as e:
            result = e

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout_duration)

    if thread.is_alive():
        # If the thread is still alive after the timeout_duration, it means it timed out
        thread.join()  # Ensure the thread terminates properly
        settings.logger.warn(f'TIMEOUT OF {int(settings.timeout)} SECONDS EXCEEDED')
        # raise TimeoutError(f"Execution of {func.__name__} timed out after {timeout_duration} seconds")
        return result

    if isinstance(result, Exception):
        # If the function raised an exception, re-raise it here
        raise result

    return result

def load_kbpath(kbpath):
    def fix_path(filename):
        full_filename = os.path.join(kbpath, filename)
        return full_filename.replace('\\', '\\\\') if os.name == 'nt' else full_filename
    return fix_path("bk.pl"), fix_path("exs.pl"), fix_path("bias.pl")

class Stats:
    def __init__(self, info = False, debug = False):
        self.exec_start = perf_counter()
        self.total_programs = 0
        self.durations = {}

    def total_exec_time(self):
        return perf_counter() - self.exec_start

    def show(self):
        message = f'Num. programs: {self.total_programs}\n'
        total_op_time = sum(summary.total for summary in self.duration_summary())

        for summary in self.duration_summary():
            percentage = int((summary.total/total_op_time)*100)
            message += f'{summary.operation}:\n\tCalled: {summary.called} times \t ' + \
                       f'Total: {summary.total:0.2f} \t Mean: {summary.mean:0.3f} \t ' + \
                       f'Max: {summary.maximum:0.3f} \t Percentage: {percentage}%\n'
        message += f'Total operation time: {total_op_time:0.2f}s\n'
        message += f'Total execution time: {self.total_exec_time():0.2f}s'
        print(message)

    def duration_summary(self):
        summary = []
        stats = sorted(self.durations.items(), key = lambda x: sum(x[1]), reverse=True)
        for operation, durations in stats:
            called = len(durations)
            total = sum(durations)
            mean = sum(durations)/len(durations)
            maximum = max(durations)
            summary.append(DurationSummary(operation.title(), called, total, mean, maximum))
        return summary

    @contextmanager
    def duration(self, operation):
        start = perf_counter()
        try:
            yield
        finally:
            end = perf_counter()
            duration = end - start

            if operation not in self.durations:
                self.durations[operation] = [duration]
            else:
                self.durations[operation].append(duration)

def format_prog(prog):
    return '\n'.join(format_rule(order_rule(rule)) for rule in order_prog(prog))

def format_prog2(prog):
    return '\n'.join(format_rule(order_rule2(rule)) for rule in order_prog(prog))

def format_literal(literal):
    args = ','.join(literal.arguments)
    return f'{literal.predicate}({args})'

def format_rule(rule):
    head, body = rule
    head_str = ''
    if head:
        head_str = format_literal(head)
    body_str = ','.join(format_literal(literal) for literal in body)
    return f'{head_str}:- {body_str}.'


def calc_prog_size(prog):
    return sum(rule_size(rule) for rule in prog)

def rule_size(rule):
    head, body = rule
    return 1 + len(body)

def reduce_prog(prog):
    def f(literal):
        return literal.predicate, literal.arguments
    reduced = {}
    for rule in prog:
        head, body = rule
        head = f(head)
        body = frozenset(f(literal) for literal in body)
        k = head, body
        reduced[k] = rule
    return reduced.values()

def order_prog(prog):
    return sorted(list(prog), key=lambda rule: (rule_is_recursive(rule), len(rule[1])))

def rule_is_recursive(rule):
    head, body = rule
    if not head:
        return False
    return any(head.predicate  == literal.predicate for literal in body if isinstance(literal, Literal))

def prog_is_recursive(prog):
    if len(prog) < 2:
        return False
    return any(rule_is_recursive(rule) for rule in prog)

def prog_has_invention(prog):
    if len(prog) < 2:
        return False
    return any(rule_is_invented(rule) for rule in prog)

def rule_is_invented(rule):
    head, body = rule
    if not head:
        return False
    return head.predicate.startswith('inv')

def order_rule2(rule, settings=None):
    head, body = rule
    return (head, sorted(body, key=lambda x: (len(x.arguments), x.predicate, x.arguments)))

# def mdl_score(score):
#     _, fn, _, fp, size = score
#     return fn + fp + size

def mdl_score(fn, fp, size):
    # _, fn, _, fp, size = score
    return fn + fp + size

def order_rule(rule, settings=None):

    if settings and settings.datalog:
        return order_rule_datalog(rule, settings)

    head, body = rule
    ordered_body = []
    grounded_variables = set()

    if head:
        if head.inputs == []:
            return rule
        grounded_variables.update(head.inputs)

    body_literals = set(body)


    while body_literals:
        selected_literal = None
        for literal in body_literals:
            if len(literal.outputs) == len(literal.arguments):
                selected_literal = literal
                break

            if not literal.inputs.issubset(grounded_variables):
                continue

            if head and literal.predicate != head.predicate:
                # find the first ground non-recursive body literal and stop
                selected_literal = literal
                break
            elif selected_literal == None:
                # otherwise use the recursive body literal
                selected_literal = literal

        if selected_literal == None:
            message = f'{selected_literal} in clause {format_rule(rule)} could not be grounded'
            raise ValueError(message)

        ordered_body.append(selected_literal)
        grounded_variables = grounded_variables.union(selected_literal.outputs)
        body_literals = body_literals.difference({selected_literal})

    return head, tuple(ordered_body)

def order_rule_datalog(rule, settings):

    def tmp_score(seen_vars, literal):
        key = []
        for x in literal.arguments:
            if x in seen_vars:
                key.append('1')
            else:
                key.append('0')
        key = ''.join(key)
        k = (literal.predicate, key)
        if k in settings.recall:
            return settings.recall[k]
        return 1000000

    head, body = rule
    ordered_body = []
    seen_vars = set()

    if head:
        seen_vars.update(head.arguments)
    body_literals = set(body)
    while body_literals:
        selected_literal = None
        for literal in body_literals:
            if set(literal.arguments).issubset(seen_vars):
                selected_literal = literal
                break

        if selected_literal == None:
            xs = sorted(body_literals, key=lambda x: tmp_score(seen_vars, x))
            selected_literal = xs[0]

        ordered_body.append(selected_literal)
        seen_vars = seen_vars.union(selected_literal.arguments)
        body_literals = body_literals.difference({selected_literal})

    # if not head:
    #     print('--')
    #     print('A',format_rule(rule))
    #     print('B',format_rule((head, ordered_body)))

    return head, tuple(ordered_body)

def print_prog_score(prog, score, noisy):
    tp, fn, tn, fp, size = score
    precision = 'n/a'
    if (tp+fp) > 0:
        precision = f'{tp / (tp+fp):0.2f}'
    recall = 'n/a'
    if (tp+fn) > 0:
        recall = f'{tp / (tp+fn):0.2f}'
    print('*'*10 + ' SOLUTION ' + '*'*10)
    if noisy:
        print(f'Precision:{precision} Recall:{recall} TP:{tp} FN:{fn} TN:{tn} FP:{fp} Size:{size} MDL:{size+fn+fp}')
    else:
      print(f'Precision:{precision} Recall:{recall} TP:{tp} FN:{fn} TN:{tn} FP:{fp} Size:{size}')  
    print(format_prog(order_prog(prog)))
    print('*'*30)

class DurationSummary:
    def __init__(self, operation, called, total, mean, maximum):
        self.operation = operation
        self.called = called
        self.total = total
        self.mean = mean
        self.maximum = maximum

def flatten(xs):
    return [item for sublist in xs for item in sublist]

class Settings:
    def __init__(self, cmd_line=False, info=True, debug=False, show_stats=False, bkcons=False, max_literals=MAX_LITERALS, timeout=TIMEOUT, quiet=False, eval_timeout=EVAL_TIMEOUT, max_examples=MAX_EXAMPLES, max_body=MAX_BODY, max_rules=MAX_RULES, max_vars=MAX_VARS, functional_test=False, kbpath=False, ex_file=False, bk_file=False, bias_file=False, datalog=False, showcons=False, no_bias=False, order_space=False, noisy=False, batch_size=BATCH_SIZE, solver='rc2', anytime_solver=None, anytime_timeout=ANYTIME_TIMEOUT):

        if cmd_line:
            args = parse_args()
            self.bk_file, self.ex_file, self.bias_file = load_kbpath(args.kbpath)
            quiet = args.quiet
            debug = args.debug
            show_stats = args.stats
            bkcons = args.bkcons
            max_literals = args.max_literals
            timeout = args.timeout
            eval_timeout = args.eval_timeout
            max_examples = MAX_EXAMPLES
            max_body = args.max_body
            max_vars = args.max_vars
            max_rules = args.max_rules
            functional_test = args.functional_test
            datalog = args.datalog
            showcons = args.showcons
            no_bias = args.no_bias
            order_space = args.order_space
            noisy = args.noisy
            batch_size = args.batch_size
            solver = args.solver
            anytime_solver = args.anytime_solver
            anytime_timeout = args.anytime_timeout
        else:
            if kbpath:
                self.bk_file, self.ex_file, self.bias_file = load_kbpath(kbpath)
            else:
                self.ex_file = ex_file
                self.bk_file = bk_file
                self.bias_file = bias_file

        self.logger = logging.getLogger("popper")

        if quiet:
            pass
        elif debug:
            log_level = logging.DEBUG
            logging.basicConfig(format='%(asctime)s %(message)s', level=log_level, datefmt='%H:%M:%S')
        elif info:
            log_level = logging.INFO
            logging.basicConfig(format='%(asctime)s %(message)s', level=log_level, datefmt='%H:%M:%S')

        self.info = info
        self.debug = debug
        self.stats = Stats(info=info, debug=debug)
        self.stats.logger = self.logger
        self.show_stats = show_stats
        self.bkcons = bkcons
        self.datalog = datalog
        self.showcons = showcons
        # self.aggressive = aggressive
        self.max_literals = max_literals
        self.functional_test = functional_test
        self.timeout = timeout
        self.eval_timeout = eval_timeout
        self.max_examples = max_examples
        self.max_body = max_body
        self.max_vars = max_vars
        self.max_rules = max_rules
        self.no_bias = no_bias
        self.order_space = order_space
        self.noisy = noisy
        self.batch_size = batch_size
        self.solver = solver
        self.anytime_solver = anytime_solver
        self.anytime_timeout = anytime_timeout

        self.recall = {}
        self.solution = None
        self.best_prog_score = None

        solver = clingo.Control(['-Wnone'])
        with open(self.bias_file) as f:
            solver.add('bias', [], f.read())
        solver.add('bias', [], """
            #defined body_literal/4.
            #defined clause/1.
            #defined clause_var/2.
            #defined var_type/3.
            #defined body_size/2.
            #defined recursive/0.
            #defined var_in_literal/4.
        """)
        solver.ground([('bias', [])])

        self.recursion_enabled = False
        for x in solver.symbolic_atoms.by_signature('enable_recursion', arity=0):
            self.recursion_enabled = True

        self.pi_enabled = False
        for x in solver.symbolic_atoms.by_signature('enable_pi', arity=0):
            self.pi_enabled = True

        # read directions from bias file when there is no PI
        if not self.pi_enabled:
            directions = defaultdict(lambda: defaultdict(lambda: '?'))
            for x in solver.symbolic_atoms.by_signature('direction', arity=2):
                pred = x.symbol.arguments[0].name
                for i, y in enumerate(x.symbol.arguments[1].arguments):
                    y = y.name
                    if y == 'in':
                        arg_dir = '+'
                    elif y == 'out':
                        arg_dir = '-'
                    directions[pred][i] = arg_dir
            self.directions = directions

        self.max_arity = 0
        for x in solver.symbolic_atoms.by_signature('head_pred', arity=2):
            self.max_arity = max(self.max_arity, x.symbol.arguments[1].number)

            if not self.pi_enabled:
                head_pred = x.symbol.arguments[0].name
                head_arity = x.symbol.arguments[1].number
                head_args = tuple(chr(ord('A') + i) for i in range(head_arity))

                head_modes = tuple(self.directions[head_pred][i] for i in range(head_arity))
                self.head_literal = Literal(head_pred, head_args, head_modes)

        for x in solver.symbolic_atoms.by_signature('max_body', arity=1):
            self.max_body = x.symbol.arguments[0].number

        for x in solver.symbolic_atoms.by_signature('max_vars', arity=1):
            self.max_vars = x.symbol.arguments[0].number

        self.max_rules = None
        for x in solver.symbolic_atoms.by_signature('max_clauses', arity=1):
            self.max_rules = x.symbol.arguments[0].number


        self.body_preds = set()
        for x in solver.symbolic_atoms.by_signature('body_pred', arity=2):
            pred = x.symbol.arguments[0].name
            arity = x.symbol.arguments[1].number
            self.body_preds.add((pred, arity))
            self.max_arity = max(self.max_arity, arity)

        arg_lookup = {i:chr(ord('A') + i) for i in range(100)}
        self.cached_atom_args = {}
        for i in range(1, self.max_arity+1):
            for args in permutations(range(0, self.max_vars), i):
                k = tuple(clingo.Number(x) for x in args)
                v = tuple(arg_lookup[x] for x in args)
                self.cached_atom_args[k] = v

        if not self.pi_enabled:
            self.body_modes = {}
            self.cached_literals = {}
            for pred, arity in self.body_preds:
                self.body_modes[pred] = tuple(directions[pred][i] for i in range(arity))

            for pred, arity in self.body_preds:
                for k, args in self.cached_atom_args.items():
                    if len(args) != arity:
                        continue
                    literal = Literal(pred, args, self.body_modes[pred])
                    self.cached_literals[(pred, k)] = literal

            pred = self.head_literal.predicate
            arity = self.head_literal.arity
            self.body_modes[pred] = tuple(directions[pred][i] for i in range(arity))
            for k, args in self.cached_atom_args.items():
                if len(args) != arity:
                    continue
                literal = Literal(pred, args, self.body_modes[pred])
                self.cached_literals[(pred, k)] = literal

        if self.max_rules == None:
            if self.recursion_enabled or self.pi_enabled:
                self.max_rules = max_rules
            else:
                self.max_rules = 1


        self.head_types, self.body_types = load_types(self)

        self.single_solve = not (self.recursion_enabled or self.pi_enabled)

        self.logger.debug(f'Max rules: {self.max_rules}')
        self.logger.debug(f'Max vars: {self.max_vars}')
        self.logger.debug(f'Max body: {self.max_body}')

        self.single_solve = not (self.recursion_enabled or self.pi_enabled)


    def print_incomplete_solution2(self, prog, tp, fn, tn, fp, size):
        self.logger.info('*'*20)
        self.logger.info('New best hypothesis:')
        if self.noisy:
            self.logger.info(f'tp:{tp} fn:{fn} tn:{tn} fp:{fp} size:{size} mdl:{size+fn+fp}')
        else:
            self.logger.info(f'tp:{tp} fn:{fn} tn:{tn} fp:{fp} size:{size}')
        for rule in order_prog(prog):
            self.logger.info(format_rule(order_rule(rule)))
        self.logger.info('*'*20)



# TODO: THIS CHECK IS NOT COMPLETE
# IT DOES NOT ACCOUNT FOR VARIABLE RENAMING
# R1 = (None, frozenset({('c3', ('A',)), ('c2', ('A',))}))
# R2 = (None, frozenset({('c3', ('B',)), ('c2', ('B',), true_value(A,B))}))
def rule_subsumes(r1, r2):
    # r1 subsumes r2 if r1 is a subset of r2
    h1, b1 = r1
    h2, b2 = r2
    if h1 != None and h2 == None:
        return False
    return b1.issubset(b2)

def theory_subsumes(prog1, prog2):
    # P1 subsumes P2 if for every rule R2 in P2 there is a rule R1 in P1 such that R1 subsumes R2
    return all(any(rule_subsumes(r1, r2) for r1 in prog1) for r2 in prog2)


def load_types(settings):
    enc = """
#defined clause/1.
#defined clause_var/2.
#defined var_type/3."""
    # solver = clingo.Control()
    solver = clingo.Control(['-Wnone'])
    with open(settings.bias_file) as f:
        solver.add('bias', [], f.read())
    solver.add('bias', [], enc)
    solver.ground([('bias', [])])

    for x in solver.symbolic_atoms.by_signature('head_pred', arity=2):
        head_pred = x.symbol.arguments[0].name
        head_arity = x.symbol.arguments[1].number

    head_types = None
    body_types = {}
    for x in solver.symbolic_atoms.by_signature('type', arity=2):
        pred = x.symbol.arguments[0].name
        # xs = (str(t) for t in )
        xs = [y.name for y in x.symbol.arguments[1].arguments]
        if pred == head_pred:
            head_types = xs
        else:
            body_types[pred] = xs

    return head_types, body_types

def bias_order(settings, max_size):

    if not (settings.no_bias or settings.order_space):
        return [(size_literals, settings.max_vars, settings.max_rules, None) for size_literals in range(1, max_size)]

    # if settings.search_order is None:
    ret = []
    predicates = len(settings.body_preds) + 1
    arity = settings.max_arity
    min_rules = settings.max_rules
    if settings.no_bias:
        min_rules = 1
    for size_rules in range(min_rules, settings.max_rules+1):
        max_size = (1 + settings.max_body) * size_rules
        for size_literals in range(1, max_size+1):
            # print(size_literals)
            minimum_vars = settings.max_vars
            if settings.no_bias:
                minimum_vars = 1
            for size_vars in range(minimum_vars, settings.max_vars+1):
                # FG We should not search for configurations with more variables than the possible variables for the number of litereals considered
                # There must be at least one variable repeated, otherwise all the literals are disconnected
                max_possible_vars = (size_literals * arity) - 1
                # print(f'size_literals:{size_literals} size_vars:{size_vars} size_rules:{size_rules} max_possible_vars:{max_possible_vars}')
                if size_vars > max_possible_vars:
                    break

                hspace = comb(predicates * pow(size_vars, arity), size_literals)

                # AC @ FG: handy code to skip pointless unsat calls
                if hspace == 0:
                    continue
                if size_rules > 1 and size_literals < 5:
                    continue
                ret.append((size_literals, size_vars, size_rules, hspace))

    if settings.order_space:
        ret.sort(key=lambda tup: (tup[3],tup[0]))

    #for x in ret:
    #    print(x)

    settings.search_order = ret
    return settings.search_order


import os
# AC: I do not know what this code below really does, but it works
class suppress_stdout_stderr(object):
    '''
    A context manager for doing a "deep suppression" of stdout and stderr in
    Python, i.e. will suppress all print, even if the print originates in a
    compiled C/Fortran sub-function.
       This will not suppress raised exceptions, since exceptions are printed
    to stderr just before a script exits, and after the context manager has
    exited (at least, I think that is why it lets exceptions through).

    '''
    def __init__(self):
        # Open a pair of null files
        self.null_fds =  [os.open(os.devnull,os.O_RDWR) for x in range(2)]
        # Save the actual stdout (1) and stderr (2) file descriptors.
        self.save_fds = [os.dup(1), os.dup(2)]

    def __enter__(self):
        # Assign the null pointers to stdout and stderr.
        os.dup2(self.null_fds[0],1)
        os.dup2(self.null_fds[1],2)

    def __exit__(self, *_):
        # Re-assign the real stdout/stderr back to (1) and (2)
        os.dup2(self.save_fds[0],1)
        os.dup2(self.save_fds[1],2)
        # Close all file descriptors
        for fd in self.null_fds + self.save_fds:
            os.close(fd)
