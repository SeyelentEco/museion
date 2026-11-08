"""calculate: arithmetic and unit conversion, exactly.

The only arithmetic an agent may report. calc.py is a hand-written parser,
never Python's eval(): no code runs, whatever the model writes.
"""

from museion import calc
from museion.agent_tools import READ, Tool, ToolError
from museion.answer import Calculation

DESCRIPTION = (
    "Evaluate an arithmetic expression exactly. Supports + - * / ^ %, parentheses, "
    "sqrt, abs, round (round(x) or round(x, decimals)), floor, ceil, min, max, pow, pi, and "
    "convert(value, from_unit, to_unit) for volume (gallon, liter, quart, pint, "
    "cup, floz, tbsp, tsp, drop, ml), mass (kg, g, lb, oz), length (km, m, cm, mm, "
    "mile, yard, foot, inch) and temperature (c, f, k). Example: "
    "convert(0.25 * 55, tsp, ml)"
)


def run(args: dict, run) -> str:
    expr = args["expression"]
    try:
        result = calc.format_number(calc.evaluate(expr))
    except ValueError as e:
        # A ToolError, so a sum that failed does not count as calculated; the
        # model is told the same words either way.
        raise ToolError(f"Could not evaluate `{expr}`: {e}. Rewrite it using only numbers, "
                        "operators, and the listed functions.") from None
    run.calculations.append(Calculation(expr, result))
    return f"{expr} = {result}. Report this figure exactly; do not convert or rescale it."


TOOL = Tool(
    name="calculate",
    description=DESCRIPTION,
    parameters={"expression": {"type": "string"}},
    required=["expression"],
    risk=READ,
    run=run,
    describe=lambda a: f"calculate {a['expression']}",
)
