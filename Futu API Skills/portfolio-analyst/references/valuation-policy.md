# Valuation calculation policy

Read before calculating a target price, valuation range or sensitivity, including a simple P/E model. Reuse this policy if already loaded. Attributed provider multiples/targets used only as background do not trigger a model. General source and unit checks remain in [evidence policy](evidence-policy.md).

## Reproducible valuations and precise scenarios

Before showing a target price, valuation range or precise scenario result, **actually execute a
checkable calculation** with an available calculator/tool or one-off local computation. Keep its
real result with key inputs, source periods, units, formulas and assumptions. Do not invent a
number and then append a plausible method. A simple ratio needs no new test suite, provider,
valuation engine or persistent artifact. Keep research scratch handling within the existing host
policy and do not save account/holding research outputs by default.

Choose a method suitable for the business; banks, insurers, REITs, cyclicals and loss-making
companies must not inherit one generic DCF/P/E template. If an applicable method lacks required
inputs, give a limited qualitative result or an explicitly assumed scenario, not asserted fair
value. Do not blindly blend DCF, relative valuation and SOTP; justify any method weights.

- **Cash-flow basis:** distinguish FCFF discounted at WACC from FCFE discounted at cost of equity.
  With CapEx defined as a positive investment outflow and change in operating working capital
  defined as an increase that consumes cash: `FCFF = EBIT * (1 - tax rate) + D&A - CapEx - delta NWC`;
  `FCFE = net income + D&A - CapEx - delta NWC + net borrowing`. Check source definitions before
  applying these formulas. A cash-flow-statement working-capital contribution may have the opposite
  sign. Normalize signs explicitly; never apply absolute values silently to CapEx or working capital.
- **Discounting and terminal value:** show the horizon, cash-flow timing, discount rate basis and
  terminal method. For end-period Gordon cash flows, `TV_N = FCF_N * (1 + g) / (r - g)` and discount
  it from year N with the same model. `WACC <= g` invalidates an FCFF Gordon valuation; FCFE likewise
  requires cost of equity greater than g. Do not quietly change g to make the formula work. Explain
  risk-free rate, beta/risk premium and financing/tax assumptions where used, without defaulting
  missing inputs. Disclose the terminal value's share of the discounted model value.
- **Enterprise-to-equity bridge:** FCFF yields enterprise value; reconcile it to equity by adding
  applicable excess cash/nonoperating assets and subtracting debt, preferred claims, minority
  interests and other relevant claims, consistently with the model's lease/operating treatment.
  FCFE already yields equity value; do not subtract debt twice. Divide the correct equity value by
  the applicable diluted shares, with explicit share-class/ADR treatment. Missing material cash,
  debt, claims or shares prevents an asserted precise per-share value; documented true zero or
  demonstrated non-applicability is different from missing. Explicit assumptions may support a
  clearly labeled scenario only.
- **Relative valuation and SOTP:** a P/E multiplies matching-period per-share earnings; an enterprise
  multiple multiplies the corresponding enterprise operating metric and still needs the equity
  bridge. Never directly multiply an EV multiple by EPS. Apply consistent segment, central-cost
  and net-debt treatment in SOTP to avoid double counting.
- **Range and sensitivity:** use appropriate precision and meaningful assumption ranges. Execute
  sensitivities using the same model; the displayed base point must reproduce the displayed base
  valuation. Mark invalid parameter cells unavailable. Explain major drivers and terminal-value
  dependence instead of presenting an unsupported exact target or investment instruction.
