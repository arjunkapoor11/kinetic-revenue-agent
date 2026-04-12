# Output Formatting Standards

Covers `export.py` — the Excel model generator.

## Ticker Sheet Structure (Rows 1-39)

### Header Block (Rows 1-5)
```
Row 1:  "TICKER: KIN Base Case Operating Model"     [bold, 12pt Times New Roman]
Row 2:  "USD in Millions Unless Stated Otherwise"    [italic, 9pt]
Row 3:  Beat Cadence (%)    [label col B + blue value col C]
Row 4:  Beat Window          [label col B + blue value col C]
Row 5:  Momentum             [label col B + blue value col C]
```

### Revenue Section (Rows 7-13)
```
Row 6:  [empty]
Row 7:  Period headers  (Q1-24, Q2-24, ... FY24, Q1-25E, ...)
Row 8:  [empty]
Row 9:  Total Revenue         [bold, $M 1dp]
          Actuals: hardcoded black numbers
          Forward: formula = prior Q revenue + $ QoQ driver (row 13)
          FY: =SUM(Q1:Q4)
Row 10: % YoY                 [italic, formula]
Row 11: % QoQ                 [italic, formula, quarters only]
Row 12: $ YoY                 [italic, formula, $M 1dp]
Row 13: $ QoQ                 [italic]
          Historical: formula = this Q - prior Q (black)
          Forward: hardcoded blue — STL-derived $ QoQ driver (the assumption input)
```

### Consensus Section (Rows 15-19)
```
Row 14: [empty]
Row 15: Consensus Total Revenue  [bold]
          Reported quarters: blank (consensus not meaningful)
          Unreported with DB estimate: hardcoded black number
          FY: =SUM(Q1:Q4) only if all 4 quarters have consensus
Row 16: % YoY                    [italic, formula referencing row 15]
Row 17: % QoQ                    [italic, formula referencing row 15]
Row 18: $ YoY                    [italic, formula referencing row 15]
Row 19: $ QoQ                    [italic, formula referencing row 15]
```

### Variance Section (Rows 21-29)
```
Row 20: [empty]
Row 21: % Variance vs Consensus  [italic, formula, forward quarters only]
          Formula: =(revenue - consensus) / consensus
          Color: conditional formatting — green if positive, red if negative
Row 22: [empty]
Row 23: Implied Q+2 Guide        [bold, one column only (Q+2)]
          Formula: =P9/(1+$C$3)  — our STL revenue estimate / (1 + beat cadence)
Row 24: % YoY                    [italic, formula vs year-ago actual]
Row 25: % QoQ                    [italic, formula vs Q+1 revenue]
Row 26: $ YoY                    [italic, formula]
Row 27: $ QoQ                    [italic, formula]
Row 28: [empty]
Row 29: % Variance Guide vs Consensus  [italic, formula, one column only]
          Formula: =(guide - consensus) / consensus
          Color: conditional formatting — green if positive, red if negative
```

### Actuals vs Consensus Section (Rows 31-39)
```
Row 30: [empty]
Row 31: "Actuals vs Consensus"     [bold, section header]
Row 32: Actual Revenue             [bold, hardcoded actuals, historical only]
Row 33: Consensus Revenue          [bold, hardcoded from pre_earnings_consensus table, historical only]
Row 34: Beat / Miss ($)            [italic, formula = actual - consensus]
Row 35: Beat / Miss (%)            [italic, formula, green if positive, red if negative]
Row 36: [empty]
Row 37: Trailing 4Q Avg Beat       [italic, formula =AVERAGE(last 4 cells in row 35)]
Row 38: Trailing 8Q Avg Beat       [italic, formula =AVERAGE(last 8 cells in row 35)]
Row 39: Selected Beat Cadence      [italic, blue text, hardcoded model input]
```

## Color Coding Rules

### Font Colors
| Color | Usage |
|-------|-------|
| **Blue** (0000FF) | Hardcoded input assumptions: $ QoQ drivers, beat cadence, beat window, momentum |
| **Black** (default) | All formulas and historical actuals |
| **Green** (006100) | Positive variance (our estimate > consensus) — via conditional formatting |
| **Red** (9C0006) | Negative variance (our estimate < consensus) — via conditional formatting |

### Conditional Formatting (evaluated by Excel, not hardcoded)
- `% Variance vs Consensus` (row 21): green font if > 0, red font if < 0
- `% Variance Guide vs Consensus` (row 29): green font if > 0, red font if < 0
- `Beat / Miss %` (row 35): green font if beat >= 0, red font if beat < 0

### Font Styles
| Context | Style |
|---------|-------|
| Section headers (Revenue, Consensus, Guide) | Bold, 9pt, not italic |
| Sub-rows (% YoY, % QoQ, etc.) | Italic, 9pt |
| Period headers (row 7) | Bold, 9pt, centered |
| Title (row 1) | Bold, 12pt |
| Subtitle (row 2) | Italic, 9pt |

## Column Structure

### Time Series Layout
- Columns A-C: fixed (spacer, labels, header values)
- Column D onward: quarterly data, left to right chronologically
- Grouped by fiscal year: Q1 | Q2 | Q3 | Q4 | FY
- Shows ~2 years of history before first estimate, then forward through FY ending closest to Dec 2027

### Column Labels
- Actuals: `Q{n}-{YY}` (e.g., Q3-25)
- Estimates: `Q{n}-{YY}E` (e.g., Q3-26E)
- Fiscal year: `FY{YY}` or `FY{YY}E`

### FY Border Conventions
- **Thin** solid borders (`Side(style="thin")`)
- **Left border** on the first quarter column of each fiscal year group
- **Both left AND right borders** on every FY total column
- Applied from period header row through last data row (row 39)

### Freeze Panes
- Ticker sheets: freeze at column D, row 9 (columns A-C and rows 1-8 visible when scrolling)

## Hardcoded vs Formula Rules

### Hardcoded Values (written as numbers)
| Cell | Font | Source |
|------|------|--------|
| Historical total revenue (row 9) | Black, bold | revenue_actuals table |
| Forward $ QoQ driver (row 13) | Blue, italic | STL decomposition output |
| Forward consensus revenue (row 15) | Black, bold | consensus_estimates table |
| Pre-earnings consensus (row 33) | Black, bold | pre_earnings_consensus table |
| Historical actual revenue (row 32) | Black, bold | revenue_actuals table |
| Selected Beat Cadence (row 39) | Blue, italic | Beat cadence computation |

### Formula-Driven Values
| Cell | Formula |
|------|---------|
| Forward total revenue (row 9) | `= prior Q revenue + $ QoQ driver` |
| % QoQ (row 11) | `= (this Q - prior Q) / prior Q` |
| % YoY (row 10) | `= (this Q - same Q prior year) / same Q prior year` |
| $ QoQ historical (row 13) | `= this Q revenue - prior Q revenue` |
| $ YoY (row 12) | `= this Q revenue - same Q prior year revenue` |
| Consensus % YoY/QoQ/$ (rows 16-19) | Formulas referencing consensus row 15 |
| % Variance vs Consensus (row 21) | `= (revenue - consensus) / consensus` |
| Implied Q+2 Guide (row 23) | `= Q+2 revenue / (1 + beat cadence %)` |
| % Variance Guide vs Consensus (row 29) | `= (guide - consensus) / consensus` |
| Beat / Miss $ (row 34) | `= actual - consensus` |
| Beat / Miss % (row 35) | `= (actual - consensus) / consensus` |
| Trailing 4Q Avg Beat (row 37) | `= AVERAGE(last 4 cells in row 35)` |
| Trailing 8Q Avg Beat (row 38) | `= AVERAGE(last 8 cells in row 35)` |
| FY totals | `= SUM(Q1:Q4)` for both revenue and consensus |

## Cell Comments
- **Anomalous historical quarters** (row 9): transcript analysis excerpt (3-4 lines max)
- **Q+1 forward quarter** (row 9): beat cadence math (`Consensus: $X.XM × (1 + X.XX%) = $X.XM implied`)
- **Q+2-Q+4 forward quarters** (row 9): STL math (`Prior Q: $X.XM + $X.XM $ QoQ = $X.XM`)
- Author: "KIN Model"

## Summary Sheet Structure

### Fixed Columns (A-C, frozen)
- Column A: Ticker symbol
- Column B: Beat Cadence % (blue text)
- Column C: Momentum (green = ACCELERATING, black = STABLE, red = DECELERATING)

### Sort Order
Companies sorted by most recent trailing 4Q revenue, descending (largest first).

### Section 1: KIN Base Case Revenue
- `Total Revenue ($M)` header row
- One row per company — cross-sheet formula references (`='SNOW'!D9`)
- `Total Revenue` sum row — `=SUM()` formula with thin top border
- `Avg % YoY Growth` row — formula off sum row
- Then per-company blocks for: `% YoY`, `% QoQ`, `$ YoY`, `$ QoQ`
- Each block has its own `Total Revenue` sum row with thin top border

### Section 2: Consensus Revenue
- Same structure as Section 1 but referencing row 15 on ticker sheets
- Reported quarters: blank (consensus row is blank on ticker sheets)
- `% YoY`, `% QoQ`, `$ YoY`, `$ QoQ` sub-blocks

### Section 3: % Variance vs Consensus
- One row per company — formula: `(KIN revenue - consensus) / consensus`
- `Total Revenue` sum row — formula off section 1 and 2 sum rows

### Far Right: Implied Q+2 Guide (after empty column separator)
- Three columns: `Impl Guide`, `Consensus`, `Gap %`
- **All cross-sheet formula references:**
  - Implied Guide: `='SNOW'!P23` (ticker sheet row 23)
  - Consensus: `='SNOW'!P15` (ticker sheet row 15)
  - Gap %: `=IFERROR((guide - consensus) / consensus, "-")` (formula)
- Gap % uses conditional formatting: green if positive, red if negative

### Summary Formatting
- FY group borders: thin on both sides of FY columns (same as ticker sheets)
- Freeze panes: D3 (first 3 columns and first 2 rows frozen)
- No gridlines
- Times New Roman throughout
- `Total Revenue` rows have thin top border separating from last company row

## Number Formatting
| Type | Format | Example |
|------|--------|---------|
| Revenue ($M) | `#,##0.0_);(#,##0.0);"-"` | 1,284.0 or (12.5) or - |
| Percentages | `0.0%_);(0.0%);"-"` | 3.5% or (1.2%) or - |
| Dollar changes ($M) | `#,##0.0_);(#,##0.0);"-"` | 78.6 or (22.1) or - |

## Pipeline Integration
- `export.py` is Step 6 of the 7-step pipeline
- Reads all data from PostgreSQL (no API calls)
- Generates `kinetic_revenue_model.xlsx` with Summary sheet + 48 ticker sheets
- Step 7 (`post_to_slack`) reads guide signals from DB and posts summary to Slack
