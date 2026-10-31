"""A small, safe calculator for the `calculate` tool.

Arithmetic, a few functions, and unit conversion -- and nothing else. No code
runs: this is a hand-written parser, never Python's eval(). A model that reads
"1/4 teaspoon per gallon" from a source and then multiplies by 55 in its head
gets it wrong often enough that the multiplication must not be its job; this
is where the number comes from.

Grammar (recursive descent):
  expr   := term (('+'|'-') term)*
  term   := power (('*'|'/'|'%') power)*
  power  := unary ('^' power)?
  unary  := '-' unary | atom
  atom   := number | ident '(' args ')' | ident | '(' expr ')'

The arithmetic follows IEEE floating point the way the Rust version did: an
operation that overflows or has no real answer (the square root of a negative
number, say) gives infinity or NaN rather than an error, and only a final
result that is not a finite number is refused. The helpers at the bottom
exist because Python's math module raises exceptions in those cases instead.
"""

from __future__ import annotations

import math

# Ways a person opens a question that is otherwise only arithmetic.
_PREFIXES = ("what is ", "what's ", "calculate ")

# What counts as whitespace: Unicode's White_Space, as in Rust. Python's own
# str.isspace() and str.strip() also take the control characters 0x1C-0x1F,
# which would let "1\x1c+1" through where the Rust version refused it.
_WHITESPACE = (
    "\t\n\x0b\x0c\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)


def evaluate(src: str) -> float:
    """Evaluate an expression. Raises ValueError, with a message fit to show
    the model, for anything that is not arithmetic."""
    p = _Parser(src)
    v = p.expr()
    p.skip_ws()
    if p.pos != len(p.chars):
        raise ValueError(f"unexpected '{p.chars[p.pos]}' at position {p.pos + 1}")
    if not math.isfinite(v):
        raise ValueError("result is not a finite number")
    return v


def format_number(v: float) -> str:
    """Format a result the way a person would write it: no trailing zeros, no
    float noise, and no more precision than the question deserves -- a dose of
    "67.772672 ml" is not more useful than 67.77, only harder to measure."""
    if math.isnan(v):
        return "NaN"
    if math.isfinite(v) and v == math.trunc(v) and abs(v) < 1e15:
        return str(int(v))
    a = abs(v)
    if a < 1e-6:
        return f"{v:.3g}"
    # Below 1, at least three significant figures: a fixed four decimals
    # showed 0.00004 as 0, and one drop in litres (0.00005) as twice that.
    decimals = 1 if a >= 100.0 else 2 if a >= 1.0 else max(4, 2 - math.floor(math.log10(a)))
    s = f"{v:.{decimals}f}"
    return s.rstrip("0").rstrip(".")


def _strip_question(q: str) -> str:
    t = q.strip(_WHITESPACE).rstrip("?= ")
    for prefix in _PREFIXES:
        if t.startswith(prefix):
            return t[len(prefix):]
    return t


def looks_like_arithmetic(q: str) -> bool:
    """True if the text is nothing but arithmetic -- the case that should never
    reach the model at all. "10*10" is answered without loading anything."""
    t = _strip_question(q)
    return (
        t != ""
        and all(_is_digit(c) or c in " +-*/^%().,x×÷" for c in t)
        and any(c in "+-*/^%×÷x" for c in t)
        and any(_is_digit(c) for c in t)
    )


def normalise(q: str) -> str:
    """Normalise the informal spellings a person types."""
    t = _strip_question(q)
    return t.replace("×", "*").replace("x", "*").replace("÷", "/").replace(",", "")


def _is_digit(c: str) -> bool:
    """An ASCII digit. str.isdigit() would also accept '²' and other
    characters no expression should contain."""
    return "0" <= c <= "9"


class _Parser:
    def __init__(self, src: str) -> None:
        self.chars = src
        self.pos = 0

    def peek(self) -> str | None:
        return self.chars[self.pos] if self.pos < len(self.chars) else None

    def skip_ws(self) -> None:
        while self.pos < len(self.chars) and self.chars[self.pos] in _WHITESPACE:
            self.pos += 1

    def eat(self, c: str) -> bool:
        self.skip_ws()
        if self.peek() == c:
            self.pos += 1
            return True
        return False

    def expr(self) -> float:
        v = self.term()
        while True:
            if self.eat("+"):
                v += self.term()
            elif self.eat("-"):
                v -= self.term()
            else:
                return v

    def term(self) -> float:
        v = self.power()
        while True:
            if self.eat("*"):
                v *= self.power()
            elif self.eat("/"):
                d = self.power()
                if d == 0.0:
                    raise ValueError("division by zero")
                v /= d
            elif self.eat("%"):
                d = self.power()
                if d == 0.0:
                    raise ValueError("division by zero")
                v = _fmod(v, d)
            else:
                return v

    def power(self) -> float:
        base = self.unary()
        if self.eat("^"):
            # Right-associative: 2^3^2 is 2^9.
            return _powf(base, self.power())
        return base

    def unary(self) -> float:
        # Note that the minus binds tighter than '^' here, so -2^2 is 4, as it
        # was in the Rust version.
        if self.eat("-"):
            return -self.unary()
        self.eat("+")
        return self.atom()

    def atom(self) -> float:
        self.skip_ws()
        if self.eat("("):
            v = self.expr()
            if not self.eat(")"):
                raise ValueError("missing ')'")
            return v
        c = self.peek()
        if c is None:
            raise ValueError("unexpected end of expression")
        if _is_digit(c) or c == ".":
            return self.number()
        if c.isalpha():
            return self.call_or_const()
        raise ValueError(f"unexpected '{c}' at position {self.pos + 1}")

    def number(self) -> float:
        start = self.pos
        while self.pos < len(self.chars) and (_is_digit(self.chars[self.pos]) or self.chars[self.pos] in "._"):
            self.pos += 1
        s = self.chars[start:self.pos].replace("_", "")
        try:
            return float(s)
        except ValueError:
            raise ValueError(f"bad number '{s}'") from None

    def ident(self) -> str:
        start = self.pos
        while self.pos < len(self.chars) and (self.chars[self.pos].isalnum() or self.chars[self.pos] == "_"):
            self.pos += 1
        return self.chars[start:self.pos].lower()

    def call_or_const(self) -> float:
        name = self.ident()
        if not self.eat("("):
            if name == "pi":
                return math.pi
            if name == "e":
                return math.e
            raise ValueError(f"unknown name '{name}'")

        # convert(value, from, to) takes unit names, which are not expressions.
        if name == "convert":
            v = self.expr()
            if not self.eat(","):
                raise ValueError("convert needs (value, from_unit, to_unit)")
            self.skip_ws()
            from_unit = self.ident()
            if not self.eat(","):
                raise ValueError("convert needs (value, from_unit, to_unit)")
            self.skip_ws()
            to_unit = self.ident()
            if not self.eat(")"):
                raise ValueError("missing ')'")
            return _convert(v, from_unit, to_unit)

        args: list[float] = []
        if not self.eat(")"):
            while True:
                args.append(self.expr())
                if self.eat(")"):
                    break
                if not self.eat(","):
                    raise ValueError(f"expected ',' or ')' in {name}(...)")

        def one() -> float:
            # Functions of one value use the first argument and ignore the rest.
            if not args:
                raise ValueError(f"{name} needs an argument")
            return args[0]

        if name == "sqrt":
            return _sqrt(one())
        if name == "abs":
            return abs(one())
        if name == "round":
            return _round_half_away(one())
        if name == "floor":
            return _whole(one(), math.floor)
        if name == "ceil":
            return _whole(one(), math.ceil)
        if name == "ln":
            return _log(one(), math.log)
        if name in ("log", "log10"):
            return _log(one(), math.log10)
        if name == "min":
            result = math.inf
            for a in args:
                result = _fmin(result, a)
            return result
        if name == "max":
            result = -math.inf
            for a in args:
                result = _fmax(result, a)
            return result
        if name == "pow":
            if len(args) != 2:
                raise ValueError("pow needs two arguments")
            return _powf(args[0], args[1])
        raise ValueError(f"unknown function '{name}'")


# Units that come up in a library like this one: water, food, distance, weight,
# temperature. Each entry is (names, kind, factor to the base unit). For
# temperature the "factor" is a code instead: 0 Celsius, 1 Fahrenheit, 2 Kelvin.
# "celsiu" is not a typo: a trailing "s" is trimmed before lookup.
_UNITS: list[tuple[tuple[str, ...], str, float]] = [
    (("liter", "litre", "l"), "volume", 1.0),
    (("milliliter", "millilitre", "ml"), "volume", 0.001),
    (("gallon", "gal"), "volume", 3.785_411_784),
    (("quart", "qt"), "volume", 0.946_352_946),
    (("pint", "pt"), "volume", 0.473_176_473),
    (("cup",), "volume", 0.236_588_236_5),
    (("fluidounce", "floz"), "volume", 0.029_573_529_6),
    (("tablespoon", "tbsp"), "volume", 0.014_786_764_8),
    (("teaspoon", "tsp"), "volume", 0.004_928_921_6),
    (("drop",), "volume", 0.000_05),
    (("kilogram", "kg"), "mass", 1.0),
    (("gram", "g"), "mass", 0.001),
    (("pound", "lb"), "mass", 0.453_592_37),
    (("ounce", "oz"), "mass", 0.028_349_523_1),
    (("meter", "metre", "m"), "length", 1.0),
    (("kilometer", "kilometre", "km"), "length", 1000.0),
    (("centimeter", "centimetre", "cm"), "length", 0.01),
    (("millimeter", "millimetre", "mm"), "length", 0.001),
    (("mile", "mi"), "length", 1609.344),
    (("yard", "yd"), "length", 0.9144),
    (("foot", "feet", "ft"), "length", 0.3048),
    (("inch", "in"), "length", 0.0254),
    (("celsiu", "c"), "temp", 0.0),
    (("fahrenheit", "f"), "temp", 1.0),
    (("kelvin", "k"), "temp", 2.0),
]


def _unit(name: str) -> tuple[str, float] | None:
    """(kind, factor) for a unit name, singular or plural, or None."""
    n = name.rstrip("s")
    for names, kind, factor in _UNITS:
        if n in names or name in names:
            return kind, factor
    return None


def _convert(v: float, from_unit: str, to_unit: str) -> float:
    found_from = _unit(from_unit)
    if found_from is None:
        raise ValueError(f"unknown unit '{from_unit}'")
    found_to = _unit(to_unit)
    if found_to is None:
        raise ValueError(f"unknown unit '{to_unit}'")
    (kind_from, factor_from), (kind_to, factor_to) = found_from, found_to
    if kind_from != kind_to:
        raise ValueError(f"cannot convert {from_unit} ({kind_from}) to {to_unit} ({kind_to})")
    if kind_from == "temp":
        if factor_from == 0:
            c = v
        elif factor_from == 1:
            c = (v - 32.0) * 5.0 / 9.0
        else:
            c = v - 273.15
        if factor_to == 0:
            return c
        if factor_to == 1:
            return c * 9.0 / 5.0 + 32.0
        return c + 273.15
    return v * factor_from / factor_to


# ── IEEE behaviour where Python's math module would raise ─────────────────────


def _powf(a: float, b: float) -> float:
    try:
        return math.pow(a, b)
    except OverflowError:
        odd_integer = math.isfinite(b) and b == math.trunc(b) and int(b) % 2 == 1
        return -math.inf if a < 0 and odd_integer else math.inf
    except ValueError:
        # Zero to a negative power is infinite; a negative number to a
        # fractional power has no real answer.
        if a == 0.0 and b < 0:
            return math.inf
        return math.nan


def _fmod(a: float, b: float) -> float:
    """The remainder with the sign of the dividend, as C and Rust compute it
    (Python's own % follows the divisor's sign instead)."""
    try:
        return math.fmod(a, b)
    except ValueError:
        return math.nan


def _sqrt(a: float) -> float:
    return math.sqrt(a) if a >= 0 or math.isnan(a) else math.nan


def _log(a: float, fn) -> float:
    if math.isnan(a) or a < 0:
        return math.nan
    if a == 0:
        return -math.inf
    return fn(a)


def _whole(a: float, fn) -> float:
    """floor or ceil as a float. Python's give an int, which loses the sign
    of a zero result (ceil(-0.5) is -0.0 in Rust and C) and cannot hold
    infinity."""
    return a if not math.isfinite(a) else math.copysign(float(fn(a)), a)


def _round_half_away(a: float) -> float:
    """Round half away from zero, as Rust's f64::round does. Python's round()
    rounds half to even, which would turn 2.5 into 2."""
    if not math.isfinite(a):
        return a
    whole = math.floor(abs(a))
    if abs(a) - whole >= 0.5:
        whole += 1
    return math.copysign(float(whole), a)


def _fmin(a: float, b: float) -> float:
    """The smaller of two numbers, ignoring NaN, as Rust's f64::min does."""
    if math.isnan(a):
        return b
    if math.isnan(b):
        return a
    return a if a < b else b


def _fmax(a: float, b: float) -> float:
    if math.isnan(a):
        return b
    if math.isnan(b):
        return a
    return a if a > b else b
