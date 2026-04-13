# KIN (Kinetic) Financial Model — Build Skill

  

This document is a complete reference for building a Kinetic-style operating model from scratch for any public company. It was developed over multiple iterative sessions building a DOCS (Doximity) model using the MDB (MongoDB) KIN template as the formatting and structural reference. A fresh Cowork session should be able to follow this guide end-to-end with no additional user input.

  

---

  

## Table of Contents

  

1. [Template Structure](#1-template-structure)

2. [Build Sequence](#2-build-sequence)

3. [Formatting Standards](#3-formatting-standards)

4. [Historical vs. Forecast Alignment](#4-historical-vs-forecast-alignment)

5. [Bugs and Failure Modes](#5-bugs-and-failure-modes)

6. [Row Insertion / Deletion — Formula Safety](#6-row-insertion--deletion--formula-safety)

7. [Visible Alpha (VA) Consensus Integration](#7-visible-alpha-va-consensus-integration)

8. [Valuation Architecture](#8-valuation-architecture)

9. [QA Checklist](#9-qa-checklist)

10. [Reference: Row Map](#10-reference-row-map)

11. [Reference: CanAlyst Column Offset](#11-reference-canalyst-column-offset)

12. [Reference: Driver Assumptions](#12-reference-driver-assumptions)

13. [Appendix: Tool Usage and Workarounds](#13-appendix-tool-usage-and-workarounds)

  

---

  

## 1. Template Structure

  

### Sheet Layout

  

Every KIN model workbook has at least three sheets (four with consensus data):

  

| Sheet | Purpose |

|---|---|

| `KIN_Model_[TICKER]` | The main operating model — all analysis lives here |

| `CanAlyst` | Raw data import from CanAlyst (read-only reference, never modify) |

| `Guidance` | Management guidance tracker data (optional, populated if available) |

| `VA` | Visible Alpha consensus data (optional, imported from VA export file) |

  

### Column Structure

  

Columns represent time periods. The layout follows a strict pattern:

  

- **Column A**: Spacer (narrow, ~2px width). Also used for hidden helper values (e.g., TODAY() for IRR).

- **Column B**: Row labels

- **Columns C onward**: Time periods

  

The time periods follow this pattern, repeating for each fiscal year:

  

```

FY  | Q1 | Q2 | Q3 | Q4 | FY  | Q1 | Q2 | Q3 | Q4 | FY  | ...

```

  

FY columns are always at positions: **3, 8, 13, 18, 23, 28, 33, 38, 43** for years with quarterly breakdowns. FY-only columns (no quarterly detail) follow at **44, 45, 46** for out-years.

  

**Historical vs. Forecast boundary**: The last fully reported quarter marks the boundary. Everything after the first estimated quarter (e.g., Q4-26E) is forecast. Typically: historical cols = 3 through last actuals column, forecast cols = first estimate column through 46.

  

**Estimate divider**: A mediumDashed right border on the last actual column (e.g., col 36 = Q3-26) visually separates actuals from estimates across ALL rows.

  

### Row Conventions

  

The model is organized into major sections, each preceded by a navy-filled (FF002060) section header row. Sub-sections use black-filled (FF000000) sub-header rows with white bold text.

  

**Row structure for each line item** follows a consistent pattern:

```

[Dollar line item]     ← BOLD, black font (formula) or green (CanAlyst link)

  % of Revenue         ← italic, indent=1

  % YoY               ← italic, indent=1

  % QoQ               ← italic, indent=1

  $ YoY               ← italic, indent=1

  $ QoQ               ← italic, indent=1

[blank separator row]  ← 1 blank row between line item groups

```

  

**Consensus rows** (when VA data is integrated) appear after the $ QoQ row for key revenue segments:

```

  $ QoQ

[blank separator]

  Consensus: [Item]    ← italic, indent=1, pulls from VA sheet

  KIN vs Consensus (%) ← italic, indent=1, =IFERROR(KIN/Consensus - 1,"")

```

  

### FY Column Formulas

  

FY columns for years with quarterly data always use `=SUM(Q1:Q4)` for dollar rows. For percentage/analytical rows, FY columns use full-year calculations (e.g., `=FY_Revenue / Prior_FY_Revenue - 1`), not averages of quarterly percentages.

  

Exception: FY2019 (col 3) has no quarterly breakdown — it pulls directly from CanAlyst.

  

Exception: FY-only forecast columns (44, 45, 46) have no quarters to sum, so dollar rows are driven by `=Prior_FY * (1 + growth_rate)` or `=Revenue * margin_pct`.

  

### Special Rows at the Top

  

```

Row 2:   Title — "[TICKER]: KIN Base Case Operating Model" (bold, 18pt, no vertical borders)

Row 3:   "USD in Millions Unless Stated Otherwise" + thick bottom border (no vertical borders)

Rows 5-10: Grouped and collapsed. Contains Print flag, Ticker, Bloomberg Ref.

Row 12:  Calendar Date (EoP) — end-of-period dates for each column, italic, M/D/YY format

Row 13:  Column headers (FY/Quarter labels)

```

  

**Rows 2 and 3** must have NO vertical borders (no thin L/R on FY columns). This is an exception to the general FY border rule.

  

**Rows 5-10** are grouped at outline level 1 and collapsed (hidden). They store metadata: Print flag (C5=1), Ticker (C6), Bloomberg reference (C7=`=C6&" US Equity"`).

  

**Row 12 Calendar Dates**: Each column gets the end-of-period date based on the company's fiscal year end. For a March FY-end company: FY cols = Mar 31, Q1 = Jun 30, Q2 = Sep 30, Q3 = Dec 31, Q4 = Mar 31. Adjust for the company's actual fiscal year end (e.g., January FY-end for a retailer). These dates are used by the IRR years calculation.

  

---

  

## 2. Build Sequence

  

Follow this exact order. Each step depends on the prior steps being complete.

  

### Phase 1: Skeleton and Data Import

  

**Step 1: Create workbook structure**

- Create the sheets: `KIN_Model_[TICKER]`, `CanAlyst`, `Guidance`, optionally `VA`

- Import the CanAlyst data file into the CanAlyst sheet (paste as values)

- Set up row 13 column headers with all time periods

- Set up row 12 with end-of-period calendar dates (italic, M/D/YY format, centered)

- Set column widths: col A = 2, col B = 40, data cols = 12

- Add title (row 2), subtitle (row 3), metadata rows 5-10 (grouped/collapsed)

  

**Step 2: Map CanAlyst offsets**

- Determine the column offset between the model sheet and CanAlyst sheet. For DOCS: `model_col + 26 = CanAlyst_col`. This offset varies by company — always verify by matching a known quarter's date.

  

**Step 3: Build the Revenue section**

- Pull historical Subscription Revenue and Other Revenue from CanAlyst using green-font cross-sheet formulas

- FY columns = SUM of quarters

- Add analytical rows: % YoY, % QoQ, $ YoY, $ QoQ below each revenue line

- Build Total Revenue = Sum of revenue components

  

### Phase 2: P&L Build-Out

  

**Step 4: Build KPIs section**

- Pull EoP Customers >$100k, Net Revenue Retention Rate from CanAlyst

  

**Step 5: Build Adjusted P&L**

- Pull COGS, S&M, R&D, G&A from CanAlyst (KIN-Adjusted basis = GAAP minus SBC)

- Build Gross Profit = Revenue - COGS

- Build Total OpEx = S&M + R&D + G&A

- Build EBIT = Gross Profit - Total OpEx

- Add full analytical rows (% of Revenue, % Margin, % YoY, % QoQ, $ Change YoY, $ Change QoQ) for each

- Include incremental margin rows (% Incremental) under major expense items with indent=1

  

**Step 6: Build EBITDA section**

- Pull D&A from CanAlyst

- EBITDA = EBIT + D&A

  

**Step 7: Build GAAP Reconciliation**

- Start from KIN-Adj EBIT, subtract Amortization and SBC to get GAAP EBIT

- Add Interest Income, Other Income, Taxes to get GAAP Net Income

- Build Non-GAAP Net Income and KIN-Adjusted Net Income (adding back tax-affected SBC)

  

**Step 8: Build EPS and Share Count**

- Pull historical diluted shares from CanAlyst

- Non-GAAP EPS = Non-GAAP NI / Diluted Shares

- KIN-Adj EPS = KIN-Adj NI / Diluted Shares

  

**Step 9: Build Reported Financials**

- Mirror the GAAP P&L from CanAlyst data (this section is purely historical reference)

  

**Step 10: Build SBC section**

- Pull SBC by function (COGS, S&M, R&D, G&A) from CanAlyst

- Calculate SBC composition percentages and SBC % of revenue by function

  

**Step 11: Build Cash Flow section**

- Pull OCF and CapEx components from CanAlyst

- FCF = OCF - CapEx

  

### Phase 3: Forward Forecast

  

**Step 12: Input forward driver assumptions**

- For each forecast period, input assumptions as blue-font hardcoded values on the driver rows:

  - Revenue growth: % YoY for each revenue component

  - Expense margins: % of Revenue for COGS, S&M, R&D, G&A

  - D&A: % of Revenue

  - SBC: % of Revenue

  - Tax rate, Amortization ($/quarter), Interest Income ($/quarter)

  - Diluted shares outstanding

  

- For quarterly forecast columns: Revenue = Prior Year Same Quarter × (1 + % YoY)

- For FY-only columns: Revenue = Prior FY × (1 + % YoY), Expenses = Revenue × % of Revenue

  

**Critical rule for FY-only expense columns**: The % of Revenue row must be a hardcoded INPUT (blue font), not a formula. If you make it `=Expense/Revenue` while also having `Expense = Revenue × %`, you create a circular reference that produces #VALUE! errors across the model.

  

**Step 13: Extend forecast formulas**

- Copy the formula patterns from historical periods into each forecast column

- Ensure FY summary columns still use SUM of quarters where quarters exist

- Ensure all analytical rows (% YoY, $ QoQ, etc.) calculate correctly in forecast periods

  

### Phase 4: Valuation

  

See Section 8 (Valuation Architecture) for the complete valuation build-out, including the historical vs. forward logic.

  

**Step 14**: Build Capital Structure (Cash, Debt, Net Cash)

**Step 15**: Build Key Financials (NTM metrics)

**Step 16**: Build Set-Up and Forward Multiples

**Step 17**: Build Valuation / Share Price / IRR

**Step 18**: Build Management Guidance Tracker

  

### Phase 5: Consensus Integration

  

**Step 19**: Import Visible Alpha data and add consensus rows. See Section 7.

  

### Phase 6: Polish

  

**Step 20**: Apply all formatting (Section 3)

**Step 21**: Add estimate divider border (mediumDashed between last actual and first estimate column)

**Step 22**: Group/collapse utility rows (5-10) and optional detail rows (e.g., EV Market, Market Cap, TEV multiples)

**Step 23**: Run full QA checklist (Section 9)

  

---

  

## 3. Formatting Standards

  

Every single one of these rules must be followed. They were established through multiple rounds of iteration and verified against the MDB reference template.

  

### Font

  

- **Times New Roman 10pt everywhere** — no exceptions, no other fonts, on every cell in the model including headers, labels, data cells, and blank cells on formatted rows.

  

### Number Formats

  

| Row Type | Format String | Example |

|---|---|---|

| Dollar amounts (1dp) | `_(* "$" #,##0.0_);_(* "$" (#,##0.0);_(* "$" "-"??_);_(@_)` | `$ 123.4` / `$ (5.6)` / `$ -` |

| Dollar amounts (2dp, for EPS/Price) | `_(* "$" #,##0.00_);_(* "$" (#,##0.00);_(* "$" "-"??_);_(@_)` | `$ 1.23` / `$ (0.45)` |

| Percentages (1dp) | `0.0%_);(0.0%);"-"` | `15.3%` / `(2.1%)` |

| Share count | `#,##0.0_);(#,##0.0);"-"` | `199.0` (no dollar sign) |

| Multiples | `0.0"x"_);(0.0"x");"-"` | `22.0x` |

| Calendar dates | `M/D/YY` | `3/31/26` |

  

**Negatives always use parentheses**, never minus signs.

  

**Watch out**: openpyxl can corrupt number format strings with excessive escape characters. Always use the exact format strings above. If you see patterns like `\(\(\(` in the stored format, it's corrupted.

  

### Color Coding

  

| Color | RGB | Usage |

|---|---|---|

| Blue | FF0000FF | Hardcoded input assumptions that the user changes for scenarios |

| Black | FF000000 | ALL formulas and calculations |

| Green | FF008000 | Cross-sheet references pulling from CanAlyst |

| White | FFFFFFFF | Text on black-filled sub-header rows |

  

**Critical rule**: In forecast periods, ONLY actual driver inputs are blue. If a cell contains a formula (even one that references a blue input), it must be black. If a cell is empty on a non-driver row, it must be black.

  

### Bold and Italic Rules

  

- **Bold**: Dollar line items (Revenue, COGS, Gross Profit, EBIT, etc.), section totals, key outputs (Price/Share, IRR, Total Enterprise Value, Equity Value)

- **Not bold**: Line items that feed into totals (Net Cash, Diluted Shares, Cum. Dividends, Years, Enterprise Value Market, Market Cap), all analytical rows, implied multiples

- **Italic**: ALL analytical/percentage rows — % YoY, % QoQ, $ YoY, $ QoQ, % of Revenue, % Margin, % Incremental Margin, Eff. Interest Rate, Net Debt / LTM Revenue, TEV / NTM Revenue (x), TEV / NTM EBIT (x), LTM Revenue, NTM Revenue. The italic applies to EVERY cell in the row, including the column B label.

  

### Indentation

  

Analytical rows use `Alignment(indent=1)` on the column B label. This puts them slightly to the right of the parent dollar line they reference. Sub-analytics (like rows nested under a total) use indent=2. In the valuation section, Implied TEV is indented (indent=1) as it's derived from the multiple above it.

  

### Borders

  

**FY Column Borders (vertical):**

- Every FY column (3, 8, 13, 18, 23, 28, 33, 38, 43, 44, 45, 46) gets thin left and right borders on EVERY row from the first data row to the last row of the model.

- This includes section header rows (navy fill), sub-header rows (black fill), blank separator rows, and data rows. NO GAPS.

- **Exception**: Rows 2 and 3 (title and subtitle) have NO vertical borders.

  

**Estimate Divider Border (vertical):**

- A `mediumDashed` right border on the last actual column (e.g., col 36 for Q3-26) runs through ALL rows from row 1 to max_row.

- This visually marks the boundary between reported actuals and estimates.

- When inserting new rows, this border must be applied to the new rows as well.

  

**Horizontal Borders:**

- Total rows (Total Revenue, Gross Profit, Total OpEx, EBIT, EBITDA, Net Income, Equity Value, Price/Share): thin TOP border across all columns including column B

- Line items before totals (e.g., Market Cap before the groupable section): thin BOTTOM border

- Horizontal borders must be continuous across ALL columns **including column B** — no gaps at the label column

- Share Price (EoP) row in valuation: DOTTED top border

  

**Section Header Fills:**

- Navy fill (FF002060) on major section headers

- Black fill (FF000000) with white bold text on sub-headers

- These header rows must also have thin L/R borders on FY columns

  

**IRR Row:**

- Warm yellow fill (FFFAF9C3)

- Thin top AND bottom borders

- Bold, indent=1

  

**Title Row (B2):**

- Bold, Times New Roman 18pt

- Row 3 gets a THICK bottom border across the full model width (cols 2–46)

  

### Row Grouping and Collapsing

  

Use openpyxl's `ws.row_dimensions.group(start, end, hidden=True, outline_level=1)` for:

- **Rows 5-10**: Metadata (Print, Ticker, Ref) — always collapsed

- **Valuation detail rows** (Enterprise Value Market, Market Cap, TEV/NTM Revenue, TEV/NTM EBIT): grouped and collapsed so the valuation section shows only the key output rows (Implied TEV, blank, Total Enterprise Value, Net Cash, Equity Value, Price/Share)

  

---

  

## 4. Historical vs. Forecast Alignment

  

### Row Alignment

  

Every analytical row must be on the SAME row number in both historical and forecast periods.

  

**The bug**: When building forecast formulas, it's easy to accidentally place `$ YoY` on the wrong row. This happens because the coder counts rows differently when working left-to-right vs. copying formulas.

  

**The fix**: Before writing ANY forecast formula, read the column B label for that row. Verify it matches what you're computing.

  

### Format Matching

  

Forecast cells must have IDENTICAL formatting to their historical counterparts:

- Same number format string

- Same font (Times New Roman 10pt)

- Same bold/italic settings

- Same border configuration

- Only difference: font COLOR (blue for inputs, black for formulas vs. green for CanAlyst links)

  

### FY Summary Columns

  

For forecast years with quarterly breakdowns:

- Dollar rows: `=SUM(Q1:Q4)` — same as historical

- % of Revenue: `=Expense_FY / Revenue_FY` — formula, not input

- % YoY: `=FY/Prior_FY - 1` — formula

  

For FY-only forecast columns (no quarterly detail):

- Dollar rows: Revenue = `Prior_FY × (1 + growth)`, Expenses = `Revenue × margin%`

- % of Revenue: HARDCODED INPUT (blue font) — because Expense depends on this %, making it a formula creates a circular reference

- % YoY: formula

  

---

  

## 5. Bugs and Failure Modes

  

These are specific bugs encountered and fixed during model builds. Each one cost significant debugging time. A fresh session should check for these proactively.

  

### Bug 1: openpyxl Does Not Update Formula References on Row Insert/Delete

  

**Symptom**: After inserting or deleting rows, formulas referencing rows below the change point still use stale row numbers.

  

**Why**: Unlike Excel, openpyxl does NOT automatically adjust formula references when rows are inserted or deleted.

  

**Fix**: Use the cross-sheet-safe formula adjuster (Section 6). Capture all formulas before the structural change, clear the sheet, rewrite with adjusted references.

  

**Prevention**: Minimize row operations. When they're unavoidable, always use the full formula capture → clear → rewrite approach from Section 6.

  

### Bug 2: Circular References in FY-Only Expense Columns

  

**Symptom**: #VALUE! errors concentrated in FY-only columns.

  

**Why**: Expense = `Revenue × % of Revenue` and % of Revenue = `Expense / Revenue` creates a circular dependency.

  

**Fix**: For FY-only expense columns, make % of Revenue a hardcoded INPUT (blue font).

  

### Bug 3: Net Cash Sign Convention

  

**Symptom**: Enterprise Value calculation wrong due to sign confusion.

  

**Why**: "Net Debt" = Debt - Cash. For a net-cash company, this is negative. The EV formula needs consistent signs.

  

**Fix**: Store Net Cash as a positive number when cash > debt. Valuation: `TEV = Market Cap - Net Cash` for historical, `Equity Value = TEV + Net Cash` for forward.

  

### Bug 4: Row Mismatch Between Historical and Forecast

  

**Symptom**: Formulas placed on wrong rows in forecast columns.

  

**Fix**: Always verify `ws.cell(row=r, column=2).value` matches the formula you're about to write.

  

### Bug 5: Terminal Year NTM EBIT = 0

  

**Symptom**: Last forecast year shows TEV = 0 because NTM EBIT is blank.

  

**Fix**: For the terminal year, use actual EBIT: `=IF(next_col_exists, NTM_EBIT, actual_EBIT)`.

  

### Bug 6: #VALUE! from Empty String Addition

  

**Symptom**: IFERROR returns `""` instead of 0, causing #VALUE! when added to numbers.

  

**Fix**: Wrap in IFERROR: `=IFERROR(Price + Dividends, "")`.

  

### Bug 7: Number Format Corruption

  

**Symptom**: Cells display excessive parentheses.

  

**Fix**: Use the tested format strings from Section 3 exactly.

  

### Bug 8: Blue Font on Formula Cells

  

**Symptom**: Derived rows show blue in forecast even though they contain formulas.

  

**Fix**: Check `str(cell.value).startswith('=')` — formulas are always black. Only literal numeric values on driver rows get blue.

  

### Bug 9: Cross-Sheet References Corrupted During Row Adjustment

  

**Symptom**: After formula adjustment, `=CanAlyst!AC33` became `=CanAlyst!AC37` — the cross-sheet row ref was incorrectly modified.

  

**Why**: A naive regex formula fixer modified ALL row references including those after `!` in cross-sheet refs.

  

**Fix**: Use the placeholder-based cross-sheet protection in the formula adjuster (Section 6). This replaces `SheetName!CellRef` patterns with placeholders before adjusting, then restores them after.

  

### Bug 10: Forward Multiple Input Cell Overwritten by Historical Fix

  

**Symptom**: Forward valuation breaks (negative prices) after applying historical implied-multiple formula to the cell that forward columns reference as the input multiple.

  

**Why**: If the 22x input lives in a data cell (e.g., E303), and you change that cell to an implied-multiple formula for historical periods, all forward `=$E$303` references now get the wrong value.

  

**Fix**: Store the forward multiple input in a non-data cell (e.g., A303 or B303) and update all forward references to point there. The data cell can then safely hold the historical implied formula.

  

**Prevention**: Before modifying any cell that serves as an absolute-reference input for other cells, check what references `$X$Y` across the sheet.

  

---

  

## 6. Row Insertion / Deletion — Formula Safety

  

This is the most dangerous operation in the entire build. openpyxl does NOT auto-adjust formulas when rows change. The approach below was developed through multiple iterations and is the only reliable method.

  

### The Approach: Capture → Clear → Rewrite

  

Do NOT use `ws.insert_rows()` or `ws.delete_rows()` and expect formulas to update. Instead:

  

1. **Capture everything** — formulas, values, styles for every cell

2. **Define a row mapper** — a function that converts old row → new row (or None if deleted)

3. **Clear the sheet**

4. **Rewrite all cells** at their new positions, adjusting formulas through the mapper

5. **Set up new rows** (inserted rows) with appropriate content and formatting

6. **Recalc and verify 0 errors**

  

### Cross-Sheet-Safe Formula Adjuster

  

This regex-based adjuster is critical. It protects cross-sheet references (e.g., `CanAlyst!AE48`, `VA!K14`, `Guidance!F10`) from being modified while still adjusting same-sheet references.

  

```python

import re

  

def adjust_formula(formula, row_mapper):

    """Adjust same-sheet row references while protecting cross-sheet refs."""

    # Step 1: Replace cross-sheet refs with placeholders

    placeholders = {}

    counter = [0]

    def replace_xref(m):

        key = f"__XREF{counter[0]}__"

        placeholders[key] = m.group(0)

        counter[0] += 1

        return key

    protected = re.sub(

        r"(?:[A-Za-z_]\w*|'[^']+')!\$?[A-Z]{1,3}\$?\d+",

        replace_xref, formula)

  

    # Step 2: Adjust same-sheet row refs

    def adjust_ref(m):

        col_part, dollar, row_str = m.group(1), m.group(2), m.group(3)

        old_row = int(row_str)

        new_row = row_mapper(old_row)

        if new_row is None:

            return f"{col_part}{dollar}{old_row}"  # deleted row, keep as-is

        return f"{col_part}{dollar}{new_row}"

    adjusted = re.sub(r'(\$?[A-Z]{1,3})(\$?)(\d+)', adjust_ref, protected)

  

    # Step 3: Restore cross-sheet refs

    for key, orig in placeholders.items():

        adjusted = adjusted.replace(key, orig)

    return adjusted

```

  

### Row Mapper Function

  

For multiple operations (deletes + inserts), apply them in sequence:

  

```python

def compute_new_row(old_row):

    r = old_row

    # Step 1: Delete row 55

    if r == 55: return None

    if r > 55: r -= 1

    # Step 2: Insert at position 12

    if r >= 12: r += 1

    # Step 3: Insert at position 309

    if r >= 309: r += 1

    return r

```

  

**Critical**: The order of operations in the mapper matters. Process deletions first, then insertions, and track how each operation shifts the threshold for subsequent operations.

  

### Full Rewrite Pattern

  

```python

# Capture all data

all_data = {}

for r in range(1, max_row + 1):

    for c in range(1, max_col + 1):

        cell = ws.cell(row=r, column=c)

        all_data[(r, c)] = {

            'value': cell.value,

            'font': copy(cell.font),

            'border': copy(cell.border),

            'fill': copy(cell.fill),

            'alignment': copy(cell.alignment),

            'number_format': cell.number_format,

        }

  

# Clear sheet

for r in range(1, new_max_row + 5):

    for c in range(1, max_col + 1):

        cell = ws.cell(row=r, column=c)

        cell.value = None

        cell.font = Font()

        cell.border = Border()

        cell.fill = PatternFill()

        cell.alignment = Alignment()

        cell.number_format = 'General'

  

# Rewrite at new positions with adjusted formulas

for old_r in range(1, max_row + 1):

    new_r = compute_new_row(old_r)

    if new_r is None: continue

    for c in range(1, max_col + 1):

        d = all_data.get((old_r, c))

        if not d: continue

        cell = ws.cell(row=new_r, column=c)

        val = d['value']

        if val and isinstance(val, str) and val.startswith('='):

            val = adjust_formula(val, compute_new_row)

        cell.value = val

        cell.font = d['font']

        cell.border = d['border']

        cell.fill = d['fill']

        cell.alignment = d['alignment']

        cell.number_format = d['number_format']

```

  

### Post-Rewrite Checklist

  

After any structural change:

1. Set up content/formatting on newly inserted rows

2. Apply the estimate divider border (mediumDashed right on the last actuals column) to new rows

3. Apply FY column thin L/R borders to new rows

4. Re-apply row grouping (openpyxl grouping is lost during full rewrite)

5. Recalc and verify 0 errors

6. Spot-check 3-5 formulas that span the insertion/deletion boundary

  

---

  

## 7. Visible Alpha (VA) Consensus Integration

  

### Overview

  

The model includes consensus estimates from Visible Alpha (VA) for comparison against KIN estimates. In MDB, these use live VA Excel plugin formulas (`=_xll.VAData(...)`). For static builds, we import a VA data export and reference it via cross-sheet formulas.

  

### VA Sheet Structure

  

The VA sheet contains data blocks imported from a VA export file. The export typically has multiple tabs (Revenue_RV, Incomestatement_IS, Balancesheet_BS, Cashflow_CF, Monitor_MON, KeyValues_KV). Import the relevant tabs as data blocks:

- **Block 1**: Revenue_RV data (rows 1-27 typically)

- **Block 2**: Incomestatement_IS data (rows 29+ typically)

  

Key VA rows for revenue consensus (row numbers are VA-sheet-specific, verify for each company):

- Subscription Revenue ($M)

- Other Revenue ($M) or equivalent segment

- Total Revenue ($M)

  

### Period Mapping

  

The VA export uses labels like `1QFY-2020`, `FY-2024`, `FY-2028(E)`. Build a mapping dictionary:

  

```python

va_label_to_model_col = {

    'FY-2019': 3,

    '1QFY-2020': 4, '2QFY-2020': 5, '3QFY-2020': 6, '4QFY-2020': 7, 'FY-2020': 8,

    # ... through all available periods ...

    'FY-2028': 44,  # or 'FY-2028(E)' depending on the export

}

```

  

Scan the VA sheet header row (row 4 typically) to find which VA column corresponds to each label, then map to model columns.

  

### Model Consensus Rows

  

Each revenue segment has two consensus rows placed after a blank separator row, following the $ QoQ analytics:

  

1. **"Consensus: [Item]"** — pulls via `=IFERROR(VA![col][row],"")`

2. **"KIN vs Consensus (%)"** — computes `=IFERROR(KIN/Consensus - 1, "")`

  

Formatting: italic, indent=1, matching analytical row style. Consensus values use dollar format; variance uses percentage format.

  

### Row Insertion for Consensus

  

Adding consensus rows requires inserting rows into the model, which triggers the full formula safety procedure (Section 6). Plan all consensus row insertions together and execute them in a single pass. Insert in reverse order (bottom-up) to simplify the row mapper.

  

---

  

## 8. Valuation Architecture

  

The valuation section has two distinct flows: one for historical periods (where market data exists) and one for forward periods (where we imply values from the model).

  

### Historical Periods (Actuals)

  

The chain flows **backward** from market data to implied multiples:

  

```

Share Price (EoP) × Diluted Shares = Market Cap

Market Cap - Net Cash = Enterprise Value (actual market TEV)

TEV / NTM EBIT = Implied Trading Multiple

```

  

Formulas (for historical columns):

- **Row: Share Price (EoP)**: `=CanAlyst!` link to end-of-period stock price

- **Row: Market Cap**: `=IFERROR(SharePrice × Shares, "")`

- **Row: Implied TEV**: `=IFERROR(MarketCap - NetCash, "")` — this IS the actual enterprise value

- **Row: (x) NTM EBIT Multiple**: `=IFERROR(ImpliedTEV / NTM_EBIT, "")` — implied from market

- **Row: Total Enterprise Value**: `=ImpliedTEV` (same as actual)

- **Row: Enterprise Value (Market)**: `=IFERROR(MarketCap, "")` — for reference

- **Row: Equity Value**: `=TEV + NetCash`

  

### Forward Periods (Estimates)

  

The chain flows **forward** from an input multiple to implied price:

  

```

NTM EBIT × Input Multiple = Implied TEV

Implied TEV + Net Cash = Equity Value

Equity Value / Diluted Shares = Price / Share

```

  

Formulas (for forward columns):

- **Row: (x) NTM EBIT Multiple**: `=$A$303` (absolute ref to input multiple, stored in col A)

- **Row: Implied TEV**: `=NTM_EBIT × Multiple`

- **Row: Total Enterprise Value**: `=ImpliedTEV`

- **Row: Enterprise Value (Market)**: `=TotalEV` (same in forward since no separate market value)

- **Row: Market Cap**: `=EquityValue` (implied, not market)

- **Row: Equity Value**: `=TEV + NetCash`

- **Row: Price / Share**: `=IFERROR(EquityValue / DilutedShares, "")`

  

### Input Multiple Storage

  

**Critical**: The input multiple (e.g., 22x) must be stored in a cell OUTSIDE the data columns (e.g., cell A303) so that historical data cells can hold implied formulas without breaking forward references. Forward columns reference `=$A$303` (absolute).

  

### Groupable Detail Rows

  

The following valuation rows are grouped and collapsed for a cleaner view:

- Enterprise Value (Market)

- Market Cap (Market)

- TEV / NTM Revenue (x)

- TEV / NTM EBIT (x)

  

These are unbolded (for EV/MktCap) and italicized (for TEV multiples). A blank separator row sits between them and Total Enterprise Value below.

  

### IRR with Live Year Calculation

  

The IRR section uses live year calculations based on calendar dates:

  

- **TODAY() cell**: Stored in cell A[Years_row] (e.g., A320), formatted M/D/YY

- **Years row formula**: `=IFERROR((col_12_date - $A$320) / 365.25, "")` for each forward column

- **IRR formula**: `=IFERROR((SharePrice_plus_Dividends / CurrentPrice)^(1/Years) - 1, "")`

  

This means IRR updates automatically as time passes, without manual year adjustments.

  

### Capital Structure: Cash and Debt Forecasting

  

- **Forward Cash**: Formula-driven FCF accumulation, NOT hardcoded blue inputs

  - Quarterly: `=prior_quarter_Cash + current_quarter_FCF`

  - FY with quarters: `=Q4_Cash` (end-of-period balance)

  - FY-only: `=prior_FY_Cash + annual_FCF`

- **Forward Debt**: Carry forward: `=prior_period_Debt`

- **Net Cash in valuation**: Formula link to Capital Structure (`=-NetDebt_row`), never hardcoded

  

### Terminal Year Handling

  

For the last forecast year, NTM EBIT cannot look forward. Use actual EBIT instead: `=IF(next_year_exists, NTM_EBIT, actual_EBIT)`.

  

---

  

## 9. QA Checklist

  

Run through every item before delivering the model.

  

### Formula Integrity

- [ ] Run recalc: `python3 mnt/.claude/skills/xlsx/scripts/recalc.py "MODEL.xlsx" 120`

- [ ] Verify output: `total_errors: 0`

- [ ] Spot-check 5 random historical cells against CanAlyst source

- [ ] Spot-check 3 FY summary columns: verify FY = SUM(Q1:Q4)

- [ ] Verify no circular references (check FY-only expense columns)

  

### Row Alignment

- [ ] For each analytical row type, verify the formula matches the label in column B across ALL columns

- [ ] Verify no formulas exist on blank separator rows

  

### Formatting

- [ ] Every cell uses Times New Roman 10pt (search for non-TNR fonts)

- [ ] All FY columns have continuous thin L/R borders from first data row to max_row with ZERO gaps

- [ ] Rows 2 and 3 have NO vertical borders

- [ ] Estimate divider (mediumDashed right border on last actuals col) runs through all rows

- [ ] All horizontal borders are continuous across cols B through last data col

- [ ] All analytical rows are italic in every cell including column B label

- [ ] Bold only on dollar line items and key outputs

- [ ] TEV / NTM multiple rows are italic (not bold)

- [ ] EV Market and Market Cap rows are unbolded

- [ ] Number formats render correctly

- [ ] Share count rows use plain number format (no dollar sign)

- [ ] EPS and Price rows use 2-decimal dollar format

- [ ] Calendar dates in row 12 are italic, centered, M/D/YY

  

### Color Coding

- [ ] Blue font ONLY on hardcoded driver inputs in forecast periods

- [ ] All formulas in forecast periods are black font

- [ ] Historical CanAlyst links are green font

- [ ] No blue font on formula cells

  

### Section Fills and Headers

- [ ] Navy fill (FF002060) on all major section headers

- [ ] Black fill (FF000000) on all sub-headers

- [ ] IRR row has warm yellow fill (FFFAF9C3) with thin top+bottom borders

- [ ] Title (B2) is bold 18pt, thick bottom border on row 3

  

### Valuation

- [ ] Net Cash in valuation is formula-linked to Capital Structure, NOT hardcoded

- [ ] Historical: implied multiple = TEV / NTM EBIT (formula)

- [ ] Forward: implied TEV = NTM EBIT × input multiple

- [ ] Input multiple stored in col A (not a data column), forward refs use absolute `=$A$row`

- [ ] Terminal year uses actual EBIT

- [ ] IRR years use live calculation: `(EoP_date - TODAY()) / 365.25`

- [ ] TODAY() formula in cell A[Years_row]

- [ ] Valuation detail rows (EV Market, MktCap, TEV multiples) grouped and collapsed

  

### Row Grouping

- [ ] Rows 5-10 grouped and collapsed (metadata)

- [ ] Valuation detail rows grouped and collapsed

  

### Consensus (if VA data present)

- [ ] Consensus rows pull from VA sheet via `=IFERROR(VA!cell, "")`

- [ ] Variance rows compute `=IFERROR(KIN/Consensus - 1, "")`

- [ ] Blank separator row between $ QoQ and consensus

- [ ] Forward consensus populated where VA data exists

  

---

  

## 10. Reference: Row Map

  

Complete row map (generalized). Actual row numbers vary by company based on the number of revenue segments, KPIs, etc. This reflects the DOCS model after all adjustments (calendar dates row, consensus rows, blank separators, valuation detail group).

  

```

Row 2:   Title — "[TICKER]: KIN Base Case Operating Model" (bold, 18pt, NO vertical borders)

Row 3:   "USD in Millions Unless Stated Otherwise" + thick bottom border (NO vertical borders)

Rows 5-10: [GROUPED/COLLAPSED] Print, Ticker, Ref

Row 12:  Calendar Date (EoP) — end-of-period dates, italic, M/D/YY

Row 13:  Column headers (FY/Quarter labels)

Row 17:  [SECTION] Operating Model

Row 18:  Revenue Build (bold header)

Row 19:  Revenue Segment 1 (e.g., Subscription Revenue)

Row 20-24: Analytics (% YoY, % QoQ, $ YoY, $ QoQ)

Row 25:  [blank separator]

Row 26:  Consensus: Segment 1 (from VA)

Row 27:  KIN vs Consensus (%)

...      [Repeat for each revenue segment]

Row N:   Total Revenue

Row N+1-5: Analytics

Row N+6: [blank separator]

Row N+7: Consensus: Total Revenue

Row N+8: KIN vs Consensus (%)

...      KPIs, Total Revenue Summary, LTM/NTM Revenue

...      [SECTION] Adjusted Financials

...      Adjusted P&L (COGS through EBIT with analytics)

...      [SECTION] EBITDA

...      [SECTION] Reconciliation to GAAP Net Income

...      [SECTION] Non-GAAP Net Income

...      [SECTION] Earnings Per Share

...      [SECTION] Share Count Bridge

...      [SECTION] Reported Financials

...      [SECTION] Stock-Based Compensation

...      [SECTION] Cash Flow

...      [SECTION] Valuation

...        [SUBHEADER] Capital Structure

...        Current Share Price, Cash, Debt, Net Debt, Interest, Ratios

...        [SUBHEADER] Key Financials

...        NTM metrics + analytics

...        Set-Up Multiples (historical)

...        [SUBHEADER] Forward Multiples

...        Share Price (EoP) — dotted top border

...        NTM KIN Adj. EBIT

...        (x) NTM EBIT Multiple — historical=implied, forward=input from col A

...        Implied TEV (indent=1)

...        [GROUPED/COLLAPSED] Enterprise Value (Market), Market Cap, TEV/NTM Rev, TEV/NTM EBIT

...        [blank separator]

...        Total Enterprise Value (bold)

...        (+) Net Cash / (-) Net Debt

...        (-) Non-Controlling Interest

...        Equity Value (bold)

...        (/) Diluted Shares

...        Price / Share (bold)

...        [blank]

...        Cum. Dividends Paid

...        Share Price + Cum. Dividends (bold)

...        [blank]

...        Years — live: (EoP_date - TODAY()) / 365.25

...        IRR — yellow fill (bold)

...        [blank]

...        [SUBHEADER] Management Guidance Tracker

...        Revenue actuals vs guide, % beat/miss

```

  

---

  

## 11. Reference: CanAlyst Column Offset

  

The CanAlyst sheet has its own column layout. To find the corresponding CanAlyst column for a model column, add the offset.

  

**This offset varies by company** — always verify by matching a known quarter's date between model and CanAlyst.

  

For DOCS: **offset = +26** (model col 5 → CanAlyst col 31 = AE)

  

Key CanAlyst rows are company-specific. For each new company build, identify the row numbers for: Revenue components, COGS, S&M, R&D, G&A, SBC by function, D&A, EPS, Shares, Stock Price, Cash, Debt, Interest Income.

  

---

  

## 12. Reference: Driver Assumptions

  

Forward assumptions are company-specific. For a new build, the user should provide these or they should be estimated from historical trends and sector comps.

  

Example (DOCS):

```

Subscription Revenue Growth (% YoY): 15% → declining to 9%

Other Revenue Growth: 10% flat

COGS % of Revenue: 8.5%

S&M % of Revenue: 16.5% → declining to 15.0%

R&D % of Revenue: 11.5% → declining to 11.0%

G&A % of Revenue: 5.0% flat

D&A % of Revenue: 1.1%

SBC % of Revenue: 17%

Effective Tax Rate: 22%

Amortization: $1.5M per quarter

Interest Income: $12M per quarter

Diluted Shares: 199M

EBIT Multiple: 22x

```

  

---

  

## 13. Appendix: Tool Usage and Workarounds

  

### Recalc Script

```bash

python3 mnt/.claude/skills/xlsx/scripts/recalc.py "path/to/model.xlsx" 120

```

Always run after any change. Returns JSON with `total_errors` and `total_formulas`.

  

### openpyxl Load Modes

- **Formula editing**: `load_workbook('file.xlsx')` — preserves formulas, this is the default

- **Reading computed values**: `load_workbook('file.xlsx', data_only=True)` — shows cached values

- **NEVER save a workbook opened with `data_only=True`** — all formulas will be permanently destroyed

  

### File Permission Workaround

  

Saving directly to the project (mounted) folder often fails with PermissionError. The reliable pattern:

  

1. Save to the working directory first: `wb.save("/sessions/.../working.xlsx")`

2. Request file delete permission: `allow_cowork_file_delete` tool

3. Remove the old file and copy: `rm "project/old.xlsx" && cp working.xlsx "project/new.xlsx"`

4. If `rm` fails, try saving with a new filename: `cp working.xlsx "project/model_v2.xlsx"`

  

### Backup Before Destructive Operations

Always create a backup before row insertions/deletions, major formula rewrites, or valuation restructuring:

```bash

cp DOCS_KIN_Model.xlsx DOCS_KIN_Model_BACKUP.xlsx

```

  

### Formula Verification After Changes

After any structural change, verify:

1. Recalc → 0 errors

2. Revenue totals match (FY = SUM of quarters)

3. Valuation chain produces reasonable prices (compare to current market price)

4. IRR values are reasonable (typically 10-40% for 1-4 year horizons)

5. Historical implied multiples match Setup Multiples section