//! A small, safe calculator for the `calculate` tool.
//!
//! Arithmetic, a few functions, and unit conversion -- and nothing else. No
//! code runs. A model that reads "1/4 teaspoon per gallon" from a source and
//! then multiplies by 55 in its head gets it wrong often enough that the
//! multiplication must not be its job; this is where the number comes from.
//!
//! Grammar (recursive descent):
//!   expr   := term (('+'|'-') term)*
//!   term   := power (('*'|'/'|'%') power)*
//!   power  := unary ('^' power)?
//!   unary  := '-' unary | atom
//!   atom   := number | ident '(' args ')' | ident | '(' expr ')'

use anyhow::{anyhow, bail, Result};

pub fn evaluate(src: &str) -> Result<f64> {
    let mut p = Parser {
        chars: src.chars().collect(),
        pos: 0,
    };
    let v = p.expr()?;
    p.skip_ws();
    if p.pos != p.chars.len() {
        bail!("unexpected '{}' at position {}", p.chars[p.pos], p.pos + 1);
    }
    if !v.is_finite() {
        bail!("result is not a finite number");
    }
    Ok(v)
}

/// Format a result the way a person would write it: no trailing zeros, no
/// float noise, and no more precision than the question deserves -- a dose
/// of "67.772672 ml" is not more useful than 67.77, only harder to measure.
pub fn format(v: f64) -> String {
    if v == v.trunc() && v.abs() < 1e15 {
        return format!("{}", v as i64);
    }
    let decimals = match v.abs() {
        a if a >= 100.0 => 1,
        a if a >= 1.0 => 2,
        _ => 4,
    };
    let s = format!("{:.*}", decimals, v);
    s.trim_end_matches('0').trim_end_matches('.').to_string()
}

/// True if the text is nothing but arithmetic -- the case that should never
/// reach the model at all. "10*10" is answered without loading anything.
pub fn looks_like_arithmetic(q: &str) -> bool {
    let t = q.trim().trim_end_matches(['?', '=', ' ']);
    let t = t
        .strip_prefix("what is ")
        .or_else(|| t.strip_prefix("what's "))
        .or_else(|| t.strip_prefix("calculate "))
        .unwrap_or(t);
    !t.is_empty()
        && t.chars().all(|c| c.is_ascii_digit() || " +-*/^%().,x×÷".contains(c))
        && t.chars().any(|c| "+-*/^%×÷x".contains(c))
        && t.chars().any(|c| c.is_ascii_digit())
}

/// Normalise the informal spellings a person types.
pub fn normalise(q: &str) -> String {
    let t = q.trim().trim_end_matches(['?', '=', ' ']);
    let t = t
        .strip_prefix("what is ")
        .or_else(|| t.strip_prefix("what's "))
        .or_else(|| t.strip_prefix("calculate "))
        .unwrap_or(t);
    t.replace(['×', 'x'], "*").replace('÷', "/").replace(',', "")
}

struct Parser {
    chars: Vec<char>,
    pos: usize,
}

impl Parser {
    fn peek(&self) -> Option<char> {
        self.chars.get(self.pos).copied()
    }
    fn skip_ws(&mut self) {
        while matches!(self.peek(), Some(c) if c.is_whitespace()) {
            self.pos += 1;
        }
    }
    fn eat(&mut self, c: char) -> bool {
        self.skip_ws();
        if self.peek() == Some(c) {
            self.pos += 1;
            true
        } else {
            false
        }
    }

    fn expr(&mut self) -> Result<f64> {
        let mut v = self.term()?;
        loop {
            if self.eat('+') {
                v += self.term()?;
            } else if self.eat('-') {
                v -= self.term()?;
            } else {
                return Ok(v);
            }
        }
    }

    fn term(&mut self) -> Result<f64> {
        let mut v = self.power()?;
        loop {
            if self.eat('*') {
                v *= self.power()?;
            } else if self.eat('/') {
                let d = self.power()?;
                if d == 0.0 {
                    bail!("division by zero");
                }
                v /= d;
            } else if self.eat('%') {
                let d = self.power()?;
                if d == 0.0 {
                    bail!("division by zero");
                }
                v %= d;
            } else {
                return Ok(v);
            }
        }
    }

    fn power(&mut self) -> Result<f64> {
        let base = self.unary()?;
        if self.eat('^') {
            let e = self.power()?;
            return Ok(base.powf(e));
        }
        Ok(base)
    }

    fn unary(&mut self) -> Result<f64> {
        if self.eat('-') {
            return Ok(-self.unary()?);
        }
        self.eat('+');
        self.atom()
    }

    fn atom(&mut self) -> Result<f64> {
        self.skip_ws();
        if self.eat('(') {
            let v = self.expr()?;
            if !self.eat(')') {
                bail!("missing ')'");
            }
            return Ok(v);
        }
        match self.peek() {
            Some(c) if c.is_ascii_digit() || c == '.' => self.number(),
            Some(c) if c.is_alphabetic() => self.call_or_const(),
            Some(c) => bail!("unexpected '{c}' at position {}", self.pos + 1),
            None => bail!("unexpected end of expression"),
        }
    }

    fn number(&mut self) -> Result<f64> {
        let start = self.pos;
        while matches!(self.peek(), Some(c) if c.is_ascii_digit() || c == '.' || c == '_') {
            self.pos += 1;
        }
        let s: String = self.chars[start..self.pos].iter().filter(|c| **c != '_').collect();
        s.parse().map_err(|_| anyhow!("bad number '{s}'"))
    }

    fn ident(&mut self) -> String {
        let start = self.pos;
        while matches!(self.peek(), Some(c) if c.is_alphanumeric() || c == '_') {
            self.pos += 1;
        }
        self.chars[start..self.pos].iter().collect::<String>().to_lowercase()
    }

    fn call_or_const(&mut self) -> Result<f64> {
        let name = self.ident();
        if !self.eat('(') {
            return match name.as_str() {
                "pi" => Ok(std::f64::consts::PI),
                "e" => Ok(std::f64::consts::E),
                other => bail!("unknown name '{other}'"),
            };
        }
        // convert(value, from, to) takes unit names, which are not expressions.
        if name == "convert" {
            let v = self.expr()?;
            if !self.eat(',') {
                bail!("convert needs (value, from_unit, to_unit)");
            }
            self.skip_ws();
            let from = self.ident();
            if !self.eat(',') {
                bail!("convert needs (value, from_unit, to_unit)");
            }
            self.skip_ws();
            let to = self.ident();
            if !self.eat(')') {
                bail!("missing ')'");
            }
            return convert(v, &from, &to);
        }
        let mut args = Vec::new();
        if !self.eat(')') {
            loop {
                args.push(self.expr()?);
                if self.eat(')') {
                    break;
                }
                if !self.eat(',') {
                    bail!("expected ',' or ')' in {name}(...)");
                }
            }
        }
        let one = |args: &[f64]| -> Result<f64> {
            args.first().copied().ok_or_else(|| anyhow!("{name} needs an argument"))
        };
        Ok(match name.as_str() {
            "sqrt" => one(&args)?.sqrt(),
            "abs" => one(&args)?.abs(),
            "round" => one(&args)?.round(),
            "floor" => one(&args)?.floor(),
            "ceil" => one(&args)?.ceil(),
            "ln" => one(&args)?.ln(),
            "log" | "log10" => one(&args)?.log10(),
            "min" => args.iter().copied().fold(f64::INFINITY, f64::min),
            "max" => args.iter().copied().fold(f64::NEG_INFINITY, f64::max),
            "pow" => {
                if args.len() != 2 {
                    bail!("pow needs two arguments");
                }
                args[0].powf(args[1])
            }
            other => bail!("unknown function '{other}'"),
        })
    }
}

/// Units that come up in a library like this one: water, food, distance,
/// weight, temperature. Each entry is (names, factor to the base unit).
fn unit(name: &str) -> Option<(&'static str, f64)> {
    let n = name.trim_end_matches('s');
    let table: &[(&[&str], &str, f64)] = &[
        (&["liter", "litre", "l"], "volume", 1.0),
        (&["milliliter", "millilitre", "ml"], "volume", 0.001),
        (&["gallon", "gal"], "volume", 3.785_411_784),
        (&["quart", "qt"], "volume", 0.946_352_946),
        (&["pint", "pt"], "volume", 0.473_176_473),
        (&["cup"], "volume", 0.236_588_236_5),
        (&["fluidounce", "floz"], "volume", 0.029_573_529_6),
        (&["tablespoon", "tbsp"], "volume", 0.014_786_764_8),
        (&["teaspoon", "tsp"], "volume", 0.004_928_921_6),
        (&["drop"], "volume", 0.000_05),
        (&["kilogram", "kg"], "mass", 1.0),
        (&["gram", "g"], "mass", 0.001),
        (&["pound", "lb"], "mass", 0.453_592_37),
        (&["ounce", "oz"], "mass", 0.028_349_523_1),
        (&["meter", "metre", "m"], "length", 1.0),
        (&["kilometer", "kilometre", "km"], "length", 1000.0),
        (&["centimeter", "centimetre", "cm"], "length", 0.01),
        (&["millimeter", "millimetre", "mm"], "length", 0.001),
        (&["mile", "mi"], "length", 1609.344),
        (&["yard", "yd"], "length", 0.9144),
        (&["foot", "feet", "ft"], "length", 0.3048),
        (&["inch", "in"], "length", 0.0254),
        (&["celsiu", "c"], "temp", 0.0),
        (&["fahrenheit", "f"], "temp", 1.0),
        (&["kelvin", "k"], "temp", 2.0),
    ];
    table
        .iter()
        .find(|(names, _, _)| names.contains(&n) || names.contains(&name))
        .map(|(_, kind, f)| (*kind, *f))
}

fn convert(v: f64, from: &str, to: &str) -> Result<f64> {
    let (kf, ff) = unit(from).ok_or_else(|| anyhow!("unknown unit '{from}'"))?;
    let (kt, ft) = unit(to).ok_or_else(|| anyhow!("unknown unit '{to}'"))?;
    if kf != kt {
        bail!("cannot convert {from} ({kf}) to {to} ({kt})");
    }
    if kf == "temp" {
        // 0 = C, 1 = F, 2 = K
        let c = match ff as u8 {
            0 => v,
            1 => (v - 32.0) * 5.0 / 9.0,
            _ => v - 273.15,
        };
        return Ok(match ft as u8 {
            0 => c,
            1 => c * 9.0 / 5.0 + 32.0,
            _ => c + 273.15,
        });
    }
    Ok(v * ff / ft)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn arithmetic_with_precedence() {
        assert_eq!(evaluate("10*10").unwrap(), 100.0);
        assert_eq!(evaluate("2 + 3 * 4").unwrap(), 14.0);
        assert_eq!(evaluate("(2 + 3) * 4").unwrap(), 20.0);
        assert_eq!(evaluate("2^3^2").unwrap(), 512.0);
        assert_eq!(evaluate("-4 + 10").unwrap(), 6.0);
        assert_eq!(evaluate("sqrt(16) + max(1, 7, 3)").unwrap(), 11.0);
    }

    #[test]
    fn the_bleach_case() {
        // 1/4 teaspoon per gallon, for a 55 gallon drum, in millilitres.
        let ml = evaluate("convert(0.25 * 55, tsp, ml)").unwrap();
        assert!((ml - 67.77).abs() < 0.05, "{ml}");
        assert_eq!(format(evaluate("4 * 4 * 30").unwrap()), "480");
    }

    #[test]
    fn units_and_temperature() {
        assert!((evaluate("convert(1, gallon, liters)").unwrap() - 3.7854).abs() < 1e-3);
        assert!((evaluate("convert(212, f, c)").unwrap() - 100.0).abs() < 1e-9);
        assert!((evaluate("convert(5, miles, km)").unwrap() - 8.0467).abs() < 1e-3);
        assert!(evaluate("convert(1, gallon, kg)").is_err());
    }

    #[test]
    fn rejects_anything_that_is_not_arithmetic() {
        assert!(evaluate("import os").is_err());
        assert!(evaluate("1/0").is_err());
        assert!(evaluate("2 +").is_err());
        assert!(evaluate("rm(1)").is_err());
    }

    #[test]
    fn spots_pure_arithmetic_questions() {
        assert!(looks_like_arithmetic("10*10"));
        assert!(looks_like_arithmetic("what is 12 x 12?"));
        assert!(looks_like_arithmetic("(3+4)/2 ="));
        assert!(!looks_like_arithmetic("how much bleach for 10 gallons"));
        assert!(!looks_like_arithmetic("2024"));
        assert_eq!(normalise("what is 12 x 12?"), "12 * 12");
    }

    #[test]
    fn formats_like_a_person() {
        assert_eq!(format(100.0), "100");
        assert_eq!(format(2.5), "2.5");
        assert_eq!(format(67.772672), "67.77");
        assert_eq!(format(343.75), "343.8");
        assert_eq!(format(1.0 / 3.0), "0.3333");
    }
}
