# BugHound Mini Model Card (Reflection)

Fill this out after you run BugHound in **both** modes (Heuristic and Gemini).

> All observations below come from real runs: Heuristic mode, offline mock clients, and Gemini mode using `gemini-3.5-flash-lite` at temperature 0.2.

---

## 1) What is this system?

**Name:** BugHound
**Purpose:** Analyze a Python snippet, propose a fix, and run reliability checks before suggesting whether the fix should be auto-applied.

**Intended users:** Students learning agentic workflows and AI reliability concepts. BugHound is a teaching tool, not a production code-modification system. Its "auto-fix" verdict is a recommendation shown in the UI. It never writes to files.

---

## 2) How does it work?

`BugHoundAgent.run()` runs five steps and logs each decision to the Agent Trace:

1. **PLAN**: fixed plan: scan, then propose a fix, then assess risk. (No real planning yet; the plan is the same every time.)
2. **ANALYZE** (`analyze`): decides *what problems exist*.
   - **Heuristics** run when there is no LLM client (`_can_call_llm()` is false). They are three regex/substring rules: `print(` → Code Quality/Low, bare `except:` → Reliability/High, `TODO` → Maintainability/Medium.
   - **Gemini** runs when a client exists. The model must return a JSON array of `{type, severity, msg}`. The agent tries `json.loads`, then extracts the first `[...]`, then validates each item (`_normalize_issues`). It **falls back to heuristics** if the API call errors, the output isn't parseable, or no item survives validation.
3. **ACT** (`propose_fix`): decides *how to change the code*.
   - No issues → return the original unchanged.
   - **Heuristic fixer**: rewrites `except:` → `except Exception as e:` plus a comment, and `print(` → `logging.info(` plus `import logging`.
   - **Gemini fixer**: the model returns the full rewritten code. Code fences are stripped. The output must be valid Python and keep every top-level function/class, or the agent falls back to the heuristic fixer (guardrail added in Part 4).
4. **TEST** (`reliability/risk_assessor.assess_risk`): decides *whether the change is safe*. It starts at 100 and deducts points for issue severity and for structural signals (much shorter code, removed `return`, removed `print`/`raise`, modified bare except). The score maps to low/medium/high.
5. **REFLECT**: `should_autofix` is true only when the level is **low** (score ≥ 75). Otherwise the agent recommends human review.

**Fix applied in Part 1:** the app's "Heuristic only" mode used to pass a `MockClient`. That made the agent think an LLM was available: the trace said "Using LLM analyzer", and the agent accepted MockClient's placeholder `# MockClient: no rewrite available` as the "fixed code". Heuristic mode now passes `client=None`, so the trace correctly says "Using heuristic analyzer (offline mode)".

---

## 3) Inputs and outputs

**Inputs tested:**

| Input | Shape |
|---|---|
| `sample_code/cleanish.py` | Short function, already uses `logging` |
| `sample_code/print_spam.py` | Function with 3 `print` calls and a `verbose` flag |
| `sample_code/flaky_try_except.py` | File read in `try` with a bare `except:` returning `None` |
| `sample_code/mixed_issues.py` | `TODO` comment, `print`, `x / y` inside bare `except:` returning `0` |
| Empty input | `""` (weird case) |
| Comments only | `# just a comment` (weird case) |
| `print(` inside a string literal | `msg = "call print( to debug"`, violates the print heuristic on purpose |

**Outputs (Heuristic mode, after my changes):**

| Input | Issues | Risk | Auto-fix |
|---|---|---|---|
| cleanish | none | 100 / low | YES (code unchanged) |
| print_spam | Code Quality/Low | 70 / medium | **NO** (was YES at 95 before the Part 3 change) |
| flaky_try_except | Reliability/High | 55 / medium | NO |
| mixed_issues | Low + High + Medium | 5 / high | NO |
| empty | none | 0 / high ("No fix was produced") | NO |
| comments only | none | 100 / low | YES (unchanged) |
| print in string | Code Quality/Low (false positive) | 70 / medium | **NO** (was YES at 95) |

**Outputs (Gemini mode, `gemini-3.5-flash-lite`):**

| Input | Issues Gemini found | Proposed fix | Risk | Auto-fix |
|---|---|---|---|---|
| mixed_issues | Maintainability/Low (TODO), Readability/Low (print), Reliability/High (bare except) | Deleted the TODO comment and the `print` line. **Added** an `isinstance` check that raises `TypeError`. Narrowed `except:` to `except ZeroDivisionError:` | 20 / high | NO |
| flaky_try_except | **Resource Leak/Medium** (file not closed if `read()` fails), Bare Except/High | Used `with open(...)` and `except Exception:`. Small, minimal diff | 35 / high | NO |

In both runs Gemini returned clean JSON and bare code: no markdown fences and no extra text. The trace showed "Using LLM analyzer" and "Using LLM fixer" with no fallbacks, and the new fix-validation guardrail accepted both fixes.

---

## 4) Reliability and safety rules

**Rule A: "Return statements may have been removed" (−30)**
- *Checks:* `"return"` appears in the original but nowhere in the fix.
- *Why it matters:* removing a return changes what callers receive. That breaks behavior silently.
- *False positive:* the word "return" appears only in a comment or string in the original (e.g. `# return early here`), and a legitimate fix drops that comment.
- *False negative:* it is a substring check, so a fix that keeps one `return` but deletes three others, or changes `return 0` to `return None`, passes untouched.

**Rule B: "Fixed code is much shorter than original" (−20 when < 50% of lines)**
- *Checks:* line count shrank by more than half.
- *Why it matters:* a large deletion usually means the model dropped logic or returned a fragment or placeholder.
- *False positive:* a legitimate cleanup that collapses repeated code, or deletes a long dead comment block.
- *False negative:* a fix of the *same length* that rewrites every line (over-editing) scores nothing here.

**Rule C (added in Part 3): "print() calls / raise statements removed" (−25 / −30)**
- *Checks:* the fix has fewer `print(` occurrences or fewer `raise` keywords than the original.
- *Why it matters:* these define observable behavior (stdout output, which errors reach callers). `print → logging.info` is silent at Python's default WARNING level. `logging.info("Hello", name)` is not equivalent to `print("Hello", name)`; if INFO logging is enabled, it produces a "--- Logging error ---" (not all arguments converted).
- *False positive:* code where removing a debug print really is the desired change. It will now require review.
- *False negative:* counting, not parsing. A fix that keeps the count but changes *which* exception is raised is not caught.

**Rule D: should_autofix == (level == "low")**
- This is somewhat **permissive**: a fix can auto-apply with a Low issue and no structural signals, even if it changes semantics in ways no rule detects. "No issues found" also yields auto-fix YES. That is harmless only because the code is returned unchanged.

---

## 5) Observed failure modes

1. **False positive plus unsafe confidence (print in a string).**
   Snippet: `def f():\n    msg = "call print( to debug"\n    return msg`
   The substring heuristic flagged `print(` inside a string literal. The fixer rewrote the literal to `"call logging.info( to debug"`, which **changes the function's return value**. The risk report said 95/low → **auto-fix YES**. Now blocked by Rule C (70/medium, NO). The underlying false positive remains (see improvement idea).

2. **Risky fix rated low risk (print_spam.py).**
   The heuristic fix produced `logging.info("Hello", name)`. That isn't equivalent to the original print and emits nothing by default. The risk report rated it 95/low, auto-fix YES, because the only deduction was −5 for a Low issue. Now 70/medium, NO.

3. **Format failure accepted as a fix (MockClient / any LLM).**
   `propose_fix` accepted *any* non-empty LLM output. MockClient's fixer returned `# MockClient: no rewrite available in offline mode.`, and that became the "fixed code": the entire function was deleted. The risk scorer happened to catch it (score 0–45) only because the placeholder was short and had no `return`. A Gemini reply like `"Here is the fixed code:\n..."` would be equally accepted. Now rejected by the workflow guardrail (`_fix_rejection_reason`), and the agent falls back to the heuristic fixer.

4. **Valid-JSON-but-useless analyzer output.**
   If the model returned `["bare except", "print usage"]` (valid JSON, wrong shape), every item was skipped, giving `[]`. The agent concluded "no issues" → returned the code unchanged → **auto-fix YES on buggy code**. Unknown severities like `"CRITICAL"` deducted 0 risk points. Both are fixed in Part 2.

5. **Missed issue (heuristic):** `mixed_issues.py` returns `0` on *any* exception from `x / y`. That hides the error, and the heuristic fix (`except Exception as e:` with `e` unused) keeps that behavior. No rule flags "swallowed exception" or a divide-by-zero masked as a valid result.

6. **Over-editing and behavior change by Gemini (mixed_issues.py).**
   The issue list said "TODO indicates incomplete input validation". Instead of leaving the TODO for a human, Gemini **implemented** input validation that wasn't asked for:
   ```python
   if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
       raise TypeError("Inputs must be numbers")
   ```
   This changes behavior. `compute_ratio(Decimal("1"), Decimal("2"))` and numpy integers (e.g. `np.int64`) used to work and now raise `TypeError`. `compute_ratio("a", 1)` used to return `0` and now raises. Gemini also **deleted** the TODO comment, even though it says "this comment is intentional". It **deleted** the `print` line outright instead of converting it to logging. These are three changes the prompt's "make the smallest changes" rule should have prevented.

7. **Risk report flagged it for the wrong reasons (false negative in the rules).**
   The mixed_issues fix still got auto-fix NO, but only because of issue severities and the print rule. No rule noticed that a **new `raise` was added**: my Part 3 rule only checks for *removed* raises. A fix with only Low issues that added the same `raise TypeError` would likely be auto-approved.

8. **Risk score measures the issues, not the fix.**
   For flaky_try_except.py, Gemini's fix (`with open` plus `except Exception`) is *better* than the heuristic fix, yet it scored **35/high** vs the heuristic's **55/medium**. That's only because Gemini found one more issue (Resource Leak, −20). `assess_risk` deducts points for *how bad the original code was*, not for *how risky the change is*. A more thorough analyzer makes every fix look riskier.

**Format failures:** none from Gemini in these runs. The format failures we saw came from the offline mock (failure modes 3–4), and the guardrails now handle them.

---

## 6) Heuristic vs Gemini comparison

- **Heuristics caught consistently:** exactly three patterns (print, bare except, TODO). They are deterministic but purely textual, which leads to the string-literal false positive and misses everything semantic.
- **Heuristic fixes:** mechanical regex rewrites. The bare-except rewrite hardcodes 8-space indentation, and the print rewrite changes behavior.
- **Gemini detected what heuristics couldn't:** the **resource leak** in `flaky_try_except.py` (file not closed if `read()` raises). Its messages also explained *why* each issue matters (e.g. bare except also catches `KeyboardInterrupt`/`SystemExit`). Neither mode flagged that `mixed_issues.py` returns `0` on division by zero, which disguises an error as a valid ratio.
- **Severity disagreements:** Gemini rated the TODO **Low** where heuristics say **Medium**, and labelled print as "Readability" instead of "Code Quality". The labels are free text, so `_heuristic_fix`'s `type == "Code Quality"` check would not match Gemini's issues if it ever had to fall back with them.
- **Format:** Gemini followed the JSON-only and code-only rules exactly. No fence stripping or array extraction was needed.
- **Fixes:** heuristic fixes are mechanical, keep every line, and add `import logging` plus a comment, with `e` left unused. Gemini's fixes are idiomatic (`with open`, specific `ZeroDivisionError`) but **take liberties**: on mixed_issues it deleted lines, added new validation and new `raise` behavior. Gemini was more accurate on flaky_try_except and over-edited on mixed_issues.

| File | Heuristic risk | Gemini risk |
|---|---|---|
| mixed_issues | 5 / high, NO | 20 / high, NO |
| flaky_try_except | 55 / medium, NO | 35 / high, NO |

- **Did the scorer match my intuition?** The *decision* (no auto-fix) was right in all four runs. The *scores and reasons* were not: the better flaky fix scored as riskier, and the actually dangerous change (new `raise TypeError`) was never mentioned in the reasons.

---

## 7) Human-in-the-loop decision

**Scenario:** the fix changes *which errors reach the caller*. For example, it turns `except: return None` into `except FileNotFoundError: return None`. Now other exceptions propagate. Or it adds or removes a `raise`.

- **Trigger:** the set of exception types caught, or the count of `raise`/`return` statements, differs between original and fix (compare via `ast`, not substrings). Removed `raise` is already implemented as Rule C.
- **Where:** `risk_assessor`. It is a property of the diff, and it belongs with the other risk signals, so it shows up in `reasons`. The workflow then refuses auto-fix through `should_autofix`.
- **Message:** "This fix changes error-handling behavior (exceptions that were caught may now propagate, or vice versa). Review before applying."

---

## 8) Improvement idea

**Make the heuristic analyzer and fixer AST-aware for `print`.** Use `ast.walk` to find real `Call` nodes whose function is `Name('print')` instead of `"print(" in code`, and rewrite only those calls. This is about 10 lines. It removes the string-literal false positive (failure mode 1) entirely. It can be measured with one test: `msg = "call print( to debug"` should produce **zero** issues and an unchanged fix. The heuristics stay simple and offline, but they stop editing code they don't understand.

---

## Changes made in this activity

| Part | File | Change | Test(s) |
|---|---|---|---|
| 1 | `bughound_app.py` | Heuristic mode uses `client=None` (true offline path) | (existing tests) |
| 2 | `bughound_agent.py` `_normalize_issues` | Drop items with no `msg`. Normalize severity; unknown → Medium. Non-empty list with zero valid items → fall back to heuristics | `test_llm_issue_list_with_no_valid_items_falls_back_to_heuristics`, `test_llm_unknown_severity_is_treated_conservatively` |
| 3 | `reliability/risk_assessor.py` | New signal: fewer `print(` (−25) or `raise` (−30) in the fix | `test_replacing_print_with_logging_blocks_autofix`, `test_removing_raise_blocks_autofix` |
| 4 | `bughound_agent.py` `_fix_rejection_reason` | LLM fix must be valid Python and keep all top-level defs/classes, else heuristic fallback | `test_llm_fix_that_drops_functions_falls_back_to_heuristic_fixer`, `test_llm_fix_with_invalid_python_falls_back_to_heuristic_fixer` |

All 6 new tests fail on the original code and pass with the changes (14/14 total).
