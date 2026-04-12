# Kinetic Partners Output Formatting Standards

## Excel Model Structure
- Wide time-series format: quarters run left-to-right, rows are metrics
- Column A: Empty left margin
- Column B: Row labels (metric names)
- Column H onward: Data begins — one column per quarter, annual totals interleaved
- Frozen panes: freeze rows 1-12 and columns A-B

## Header Block
- Row 1, Cell B1: "[TICKER]: KIN Base Case Operating Model" — bold, 12pt
- Row 2, Cell B2: "USD in Millions Unless Stated Otherwise" — italic, 9pt
- Row 12: Primary period labels — format Q1-26, Q2-26, Q3-26E, Q4-26E, FY26E (E suffix for estimates, no suffix for actuals)

## Metric Row Order (for each revenue line)
1. Revenue ($ values)
2. % YoY
3. % QoQ
4. $ YoY
5. $ QoQ
6. Blank spacer row

## Number Formatting
- Revenue: #,##0.0 in millions, zeros as "-"
- Percentages: 0.0%, zeros as "-"
- Negative numbers: parentheses (123.4) not minus sign

## Font
- Times New Roman, 9pt for all data cells
- Section headers: 10pt bold
- Title row: 12pt bold
- Subtitle row: 9pt italic

## Colors
- Blue text RGB(0,0,255): hardcoded inputs
- Black text: all formulas
- Section header rows: dark navy background RGB(26,31,46), white text, bold
- GUIDE ABOVE / BEAT: green cell background
- GUIDE BELOW / MISS: red cell background
- IN-LINE: yellow cell background

## Column Widths
- Column A: 3
- Column B: 38
- Data columns (quarterly): 10
- Annual total columns: 11

## Borders
- Thick bottom border on row 12 (period header row)
- Thin right border on annual total columns
- Thick right border after last actual period separating actuals from estimates

## Section Labels
- Dark navy background, white bold text
- Prefix KIN estimates with "KIN Est."
- Prefix reference rows with "Memo:"

## Guide Inference Table
- First data section after header block
- Most visually prominent element
- Header: ticker name + "| Beat Cadence: +X.X% (4Q avg)"
- Columns: Period | Our Projected Actual | Implied Guide | Consensus | Gap $ | Gap % | Signal

## Summary Sheet
Three tables side by side:
1. Beat Cadence Overview: Ticker | Beat 4Q | Beat 8Q | Selected | Changing?
2. Guide Inference Signals: Ticker | Q+1 | Q+2 | Q+3 | Q+4 | Momentum
3. Consensus Comparison: Ticker | Next Q Signal | Gap% | FY Signal | Gap% | NxFY Signal | Gap%

## Gridlines
- Gridlines OFF on all sheets: `ws.sheet_view.showGridLines = False`

## Beat vs Consensus History Section
- Placed after the revenue metric rows on each ticker sheet
- Dark navy section header: "BEAT VS CONSENSUS HISTORY"
- Data sourced from FMP `/stable/earnings` endpoint (real pre-earnings consensus, not backfilled DB data)
- Vertical table in columns B-G (not in the wide time-series data columns)
- Columns: Period | Actual Revenue ($M) | Consensus Est. ($M) | Beat $ | Beat % | Signal
- Actual Revenue and Consensus Estimate are hardcoded inputs (blue text)
- Beat $, Beat %, and Signal are live Excel formulas (black text)
- Beat $ = Actual - Consensus
- Beat % = IFERROR((Actual - Consensus) / Consensus, "-")
- Signal = IF(Beat% > 0, "BEAT", "MISS")
- Signal cells use conditional formatting: green background for BEAT, red for MISS
- Up to 8 most recent quarters shown (newest first)
- Summary stats below with live formulas:
  - Beat Rate % = COUNTIF(signals, "BEAT") / COUNTA(signals)
  - Avg Beat % (wins only) = AVERAGEIF(beat_pcts, ">0")
  - Avg Miss % (losses only) = AVERAGEIF(beat_pcts, "<0")
  - 4Q Avg Beat % = AVERAGE(first 4 beat % cells)
  - 8Q Avg Beat % = AVERAGE(all beat % cells)

## Live Excel Formulas (no hardcoded Python calculations)
- % YoY = IFERROR((current-prior_year)/prior_year, "-")
- % QoQ = IFERROR((current-prior_quarter)/prior_quarter, "-")
- $ YoY = current - prior_year
- $ QoQ = current - prior_quarter
- Annual totals = SUM(Q1:Q4)
- Guide inference = projected / (1 + beat_cadence)
- Divergence % = IFERROR((our_estimate-consensus)/consensus, "-")
- Signal = IF(gap>2%, "GUIDE ABOVE", IF(gap<-2%, "GUIDE BELOW", "IN-LINE"))
- Beat $ = actual_revenue - consensus_estimate
- Beat % = IFERROR((actual-consensus)/consensus, "-")
- Beat signal = IF(beat%>0, "BEAT", "MISS")
- Beat rate = COUNTIF(signals, "BEAT") / COUNTA(signals)
- Avg beat/miss = AVERAGEIF(beat_pcts, ">0") / AVERAGEIF(beat_pcts, "<0")
