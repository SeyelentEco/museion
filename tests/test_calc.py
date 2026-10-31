"""The calculator: the tests ported from calc.rs, then a parity table.

The calculator computes doses, so it must give the same answers as the Rust
version it replaces. The tables at the bottom are Rust's own output: each
expression was run through the Rust calc.rs (now on the archive/rust-app
branch), unchanged, and
the exact value (to the last bit), formatted text, or error message it gave is
what the Python version is held to here."""

import math
import unittest

from museion import calc


class CalcTest(unittest.TestCase):
    def test_arithmetic_with_precedence(self):
        self.assertEqual(calc.evaluate("10*10"), 100.0)
        self.assertEqual(calc.evaluate("2+3*4"), 14.0)
        self.assertEqual(calc.evaluate("(2+3)*4"), 20.0)
        self.assertEqual(calc.evaluate("2^3^2"), 512.0)
        self.assertEqual(calc.evaluate("-4+10"), 6.0)
        self.assertEqual(calc.evaluate("sqrt(16)+max(1,7,3)"), 11.0)

    def test_the_bleach_case(self):
        # 1/4 tsp per gallon, 55 gallons.
        ml = calc.evaluate("convert(0.25*55,tsp,ml)")
        self.assertLess(abs(ml - 67.77), 0.05, ml)
        self.assertEqual(calc.format_number(calc.evaluate("4*4*30")), "480")

    def test_units_and_temperature(self):
        self.assertLess(abs(calc.evaluate("convert(1,gallon,liters)") - 3.7854), 1e-3)
        self.assertLess(abs(calc.evaluate("convert(212,f,c)") - 100.0), 1e-9)
        self.assertLess(abs(calc.evaluate("convert(5,miles,km)") - 8.0467), 1e-3)
        with self.assertRaises(ValueError):
            calc.evaluate("convert(1,gallon,kg)")

    def test_rejects_anything_that_is_not_arithmetic(self):
        for src in ["import os", "1/0", "2 +", "rm(1)"]:
            with self.subTest(src=src), self.assertRaises(ValueError):
                calc.evaluate(src)

    def test_spots_pure_arithmetic_questions(self):
        self.assertTrue(calc.looks_like_arithmetic("10*10"))
        self.assertTrue(calc.looks_like_arithmetic("what is 12 x 12?"))
        self.assertTrue(calc.looks_like_arithmetic("(3+4)/2 ="))
        self.assertFalse(calc.looks_like_arithmetic("how much bleach for 10 gallons"))
        self.assertFalse(calc.looks_like_arithmetic("2024"))
        self.assertEqual(calc.normalise("what is 12 x 12?"), "12 * 12")

    def test_formats_like_a_person(self):
        self.assertEqual(calc.format_number(100.0), "100")
        self.assertEqual(calc.format_number(2.5), "2.5")
        self.assertEqual(calc.format_number(67.772672), "67.77")
        self.assertEqual(calc.format_number(343.75), "343.8")
        self.assertEqual(calc.format_number(1.0 / 3.0), "0.3333")
        # Small results keep three significant figures, not four decimals.
        self.assertEqual(calc.format_number(0.00004), "0.00004")
        self.assertEqual(calc.format_number(0.00005), "0.00005")
        self.assertEqual(calc.format_number(0.0123456), "0.0123")
        self.assertEqual(calc.format_number(0.00123456), "0.00123")
        self.assertEqual(calc.format_number(-0.000254), "-0.000254")
        self.assertEqual(calc.format_number(2.5e-9), "2.5e-09")

    def test_never_runs_code(self):
        # Python's eval() would run these; the calculator only parses.
        for src in ["__import__('os')", "open('x')", "1 if 1 else 2", "[1][0]", "2**3"]:
            with self.subTest(src=src), self.assertRaises(ValueError):
                calc.evaluate(src)


class RustParityTest(unittest.TestCase):
    def test_results_match_rust_exactly(self):
        for src, value, text in RUST_RESULTS:
            with self.subTest(src=src):
                got = calc.evaluate(src)
                # Bit for bit, so the sign of a zero counts too.
                self.assertEqual((got, math.copysign(1.0, got)), (value, math.copysign(1.0, value)))
                self.assertEqual(calc.format_number(got), text)

    def test_errors_match_rust(self):
        for src, message in RUST_ERRORS:
            with self.subTest(src=src):
                with self.assertRaises(ValueError) as caught:
                    calc.evaluate(src)
                self.assertEqual(str(caught.exception), message)

    def test_formatting_matches_rust(self):
        for value, text in RUST_FORMATS:
            with self.subTest(value=value):
                self.assertEqual(calc.format_number(value), text)

    def test_questions_match_rust(self):
        for question, arithmetic, normalised in RUST_QUESTIONS:
            with self.subTest(question=question):
                self.assertEqual(calc.looks_like_arithmetic(question), arithmetic)
                self.assertEqual(calc.normalise(question), normalised)


# (expression, the value Rust computed, Rust's calc::format of it)
RUST_RESULTS = [
    ("10*10", 100.0, "100"),
    ("2+3*4", 14.0, "14"),
    ("(2+3)*4", 20.0, "20"),
    ("2^3^2", 512.0, "512"),
    ("(2^3)^2", 64.0, "64"),
    ("-4+10", 6.0, "6"),
    ("-2^2", 4.0, "4"),
    ("2^-2", 0.25, "0.25"),
    ("--3", 3.0, "3"),
    ("- 3 * -2", 6.0, "6"),
    ("+5", 5.0, "5"),
    ("10-4-3", 3.0, "3"),
    ("100/10/5", 2.0, "2"),
    ("7%3", 1.0, "1"),
    ("-7%3", -1.0, "-1"),
    ("7.5%2", 1.5, "1.5"),
    ("1_000*3", 3000.0, "3000"),
    (".5+.25", 0.75, "0.75"),
    ("1/3", 0.3333333333333333, "0.3333"),
    ("2/3*3", 2.0, "2"),
    ("0.1+0.2", 0.30000000000000004, "0.3"),
    ("sqrt(16)+max(1,7,3)", 11.0, "11"),
    ("sqrt(2)", 1.4142135623730951, "1.41"),
    ("abs(-3.5)", 3.5, "3.5"),
    ("round(2.5)", 3.0, "3"),
    ("round(-2.5)", -3.0, "-3"),
    ("round(0.49999999999999994)", 0.0, "0"),
    ("floor(-2.5)", -3.0, "-3"),
    ("ceil(2.1)", 3.0, "3"),
    ("ln(e)", 1.0, "1"),
    ("log(1000)", 3.0, "3"),
    ("log10(0.01)", -2.0, "-2"),
    ("min(4, -2, 9)", -2.0, "-2"),
    ("pow(2, 10)", 1024.0, "1024"),
    ("sqrt(9, 100)", 3.0, "3"),
    ("PI*2", 6.283185307179586, "6.28"),
    ("2*pi", 6.283185307179586, "6.28"),
    ("e^2", 7.3890560989306495, "7.39"),
    ("4*4*30", 480.0, "480"),
    ("convert(0.25*55,tsp,ml)", 67.772672, "67.77"),
    ("convert(1, gallon, liters)", 3.785411784, "3.79"),
    ("convert(1, gal, l)", 3.785411784, "3.79"),
    ("convert(1, Gallons, L)", 3.785411784, "3.79"),
    ("convert(1, litre, quart)", 1.0566882094325938, "1.06"),
    ("convert(1000, milliliters, liter)", 1.0, "1"),
    ("convert(2, millilitre, ml)", 2.0, "2"),
    ("convert(1, qt, pint)", 2.0, "2"),
    ("convert(1, pt, cup)", 2.0, "2"),
    ("convert(1, cups, floz)", 7.999999989855794, "8"),
    ("convert(1, fluidounce, tablespoons)", 2.0, "2"),
    ("convert(1, tbsp, teaspoon)", 2.9999999999999996, "3"),
    ("convert(20, drops, tsp)", 0.20288413595379565, "0.2029"),
    ("convert(1, kilogram, pounds)", 2.2046226218487757, "2.2"),
    ("convert(1, kg, g)", 1000.0, "1000"),
    ("convert(500, grams, ounce)", 17.63698099034336, "17.64"),
    ("convert(1, lb, oz)", 16.000000014109585, "16"),
    ("convert(16, ounces, pound)", 0.9999999991181509, "1"),
    ("convert(5, miles, km)", 8.04672, "8.05"),
    ("convert(1, mi, yards)", 1760.0, "1760"),
    ("convert(1, yd, feet)", 3.0, "3"),
    ("convert(1, ft, in)", 12.000000000000002, "12"),
    ("convert(1, inch, cm)", 2.54, "2.54"),
    ("convert(1, meter, centimeters)", 100.0, "100"),
    ("convert(1, metre, millimetre)", 1000.0, "1000"),
    ("convert(1, m, mm)", 1000.0, "1000"),
    ("convert(1, kilometre, metres)", 1000.0, "1000"),
    ("convert(1, kilometer, meters)", 1000.0, "1000"),
    ("convert(10, centimetre, millimeter)", 100.0, "100"),
    ("convert(212, f, c)", 100.0, "100"),
    ("convert(100, celsius, fahrenheit)", 212.0, "212"),
    ("convert(-40, C, F)", -40.0, "-40"),
    ("convert(0, kelvin, celsius)", -273.15, "-273.1"),
    ("convert(300, k, f)", 80.33000000000004, "80.33"),
    ("convert(98.6, fahrenheit, kelvin)", 310.15, "310.1"),
    ("convert(37, c, k)", 310.15, "310.1"),
    ("convert(20, c, c)", 20.0, "20"),
    ("convert(1, mss, ft)", 3.280839895013123, "3.28"),
    ("12 * 12", 144.0, "144"),
    ("1\u00a0+\u30001", 2.0, "2"),
    ("\f2 *\u2003 3\u205f", 6.0, "6"),
]

# (expression, Rust's error message)
RUST_ERRORS = [
    ("max()", "result is not a finite number"),
    ("min()", "result is not a finite number"),
    ("pow(2)", "pow needs two arguments"),
    ("sqrt()", "sqrt needs an argument"),
    ("convert(1, foot, inches)", "unknown unit 'inches'"),
    ("convert(1, gallon, kg)", "cannot convert gallon (volume) to kg (mass)"),
    ("convert(1, c, m)", "cannot convert c (temp) to m (length)"),
    ("convert(1, parsec, m)", "unknown unit 'parsec'"),
    ("convert(1, m, furlong)", "unknown unit 'furlong'"),
    ("convert(1 m to ft)", "convert needs (value, from_unit, to_unit)"),
    ("convert(1, m)", "convert needs (value, from_unit, to_unit)"),
    ("convert(1, m, ft", "missing ')'"),
    ("1/0", "division by zero"),
    ("5%0", "division by zero"),
    ("1/(2-2)", "division by zero"),
    ("2 +", "unexpected end of expression"),
    ("", "unexpected end of expression"),
    ("   ", "unexpected end of expression"),
    ("import os", "unknown name 'import'"),
    ("rm(1)", "unknown function 'rm'"),
    ("foo", "unknown name 'foo'"),
    ("1 2", "unexpected '2' at position 3"),
    ("(1+2", "missing ')'"),
    ("1+2)", "unexpected ')' at position 4"),
    ("2*?", "unexpected '?' at position 3"),
    ("max(1;2)", "expected ',' or ')' in max(...)"),
    ("1..2", "bad number '1..2'"),
    ("1e5", "unexpected 'e' at position 2"),
    ("10^400", "result is not a finite number"),
    ("sqrt(-1)", "result is not a finite number"),
    ("ln(0)", "result is not a finite number"),
    ("(-8)^(1/3)", "result is not a finite number"),
    ("0^-1", "result is not a finite number"),
    ("12 x 12", "unexpected 'x' at position 4"),
    ("2 3", "unexpected '3' at position 3"),
    ("3,4", "unexpected ',' at position 2"),
    ("1\u001c+1", "unexpected '\u001c' at position 2"),
]

# (value, Rust's calc::format of it). The two small values are the exception:
# Rust printed 1e-05 as "0" and -4.9e-05 as "-0", which hid a dose, so they
# now keep three significant figures.
RUST_FORMATS = [
    (100.0, "100"),
    (2.5, "2.5"),
    (67.772672, "67.77"),
    (343.75, "343.8"),
    (0.3333333333333333, "0.3333"),
    (480.0, "480"),
    (-0.0, "0"),
    (1000000000000000.0, "1000000000000000"),
    (999999999999999.0, "999999999999999"),
    (1e+16, "10000000000000000"),
    (123456789.987, "123456790"),
    (2.125, "2.12"),
    (0.03125, "0.0312"),
    (100.25, "100.2"),
    (1.005, "1"),
    (1e-05, "0.00001"),
    (-1.5, "-1.5"),
    (-4.9e-05, "-0.000049"),
    (99.995, "100"),
    (0.5, "0.5"),
    (math.inf, "inf"),
    (-math.inf, "-inf"),
    (math.nan, "NaN"),
]

# (question, Rust's looks_like_arithmetic, Rust's normalise)
RUST_QUESTIONS = [
    ("10*10", True, "10*10"),
    ("what is 12 x 12?", True, "12 * 12"),
    ("(3+4)/2 =", True, "(3+4)/2"),
    ("how much bleach for 10 gallons", False, "how much bleach for 10 gallons"),
    ("2024", False, "2024"),
    ("calculate 3,000 ÷ 4", True, "3000 / 4"),
    ("what's 6×7", True, "6*7"),
    ("What is 2+2", False, "What is 2+2"),
    ("  5 - 1 ==?? ", True, "5 - 1"),
    ("x", False, "*"),
    ("1x", True, "1*"),
    ("12.5%", True, "12.5%"),
    ("what is ?", False, "what is"),
]


if __name__ == "__main__":
    unittest.main()
