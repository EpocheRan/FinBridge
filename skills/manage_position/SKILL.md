---
name: "manage-position"
description: "Procedure for opening, trading, modifying, and closing stock positions. Use for lifecycle management of stock holdings and risk-aware trade execution."
---

# Manage Position

## Scope

**Covers**
- Opening a new position tracking (tracking-only or initial trade).
- Executing Buy/Sell trades and updating cash balances.
- Modifying position attributes (name, metadata).
- Closing positions or removing them from tracking.
- Syncing position names with official ticker data.

**When to use**
- "Buy 100 shares of AAPL at 150."
- "Sell all my TSLA holdings."
- "Start tracking 600519 with ATR mode."
- "Update the quantity for my Nvidia position."

**When NOT to use**
- You only want to change the Portfolio name/cash (use `manage-portfolio`).
- you need to see market charts (use `get_ohlc_data`).
- You need to check for trade signals without executing (use `pm_check_signals`).

## Inputs

**Minimum required**
- `portfolio_id`: The ID of the owner portfolio.
- `ticker`: Stock symbol (e.g., "AAPL").
- `action`: open, buy, sell, modify, close.
- For trade: `quantity`, `price`.

**Missing-info strategy**
- If `portfolio_id` is missing, call `pm_list_portfolios` and ask the user to select.
- If `price` is missing for a trade, use `get_ohlc_data` to fetch the latest close price and confirm with the user.

## Outputs (deliverables)

1) **Transaction Summary**: Detailed breakdown of the trade (Price, Qty, Total, Resulting Cash).
2) **Position Status**: Updated position details (New Quantity, New Average Cost).
3) **Risk Update**: If applicable, the updated stop-loss or ATR parameters.

## Workflow (4 steps)

### 1) Pre-check and Risk Advice
- **Actions**: 
    - Call `pm_get_portfolio_status` to verify current holdings and cash.
    - For **Buy** trades: Call `pm_get_buying_advice` to ensure the trade fits risk parameters.
- **Checks**: Verify sufficient cash exists for Buy trades.

### 2) Execute Operation
- **Actions**:
    - For **New Ticker**: Call `pm_open_position`.
    - For **Trade**: Call `pm_execute_trade`.
    - For **Meta Update**: Call `pm_modify_position`.
- **Checks**: Tool must return `success: true`.
- **STOP**: You **MUST** stop here and wait for the tool output before proceeding to any verification or further calls.

### 3) Official Name Sync
- **Actions**: If the position is new or has an generic name:
    - Call `get_ticker_basic_info(ticker)`.
    - Call `pm_modify_position(pos_id, name=official_name)`.
- **STOP**: Wait for the tool response.

### 4) Verify State (CRITICAL)
- **Actions**:
    - **Wait for Turn**: **DO NOT** call `pm_get_portfolio_status` in the same response as any modification tool.
    - **Query**: In a **NEW response** (after receiving modification success), call `pm_get_portfolio_status`.
    - **Compare**: Verify that `quantity` matches the math (old + change) and `cash` is updated.
- **Checks**: Report the final confirmed state to the user in this second turn.

## Quality gate (required)
- All trades MUST be followed by a `pm_get_portfolio_status` call to confirm the database updated.
- Use tables to display the final state.

## Examples

**Example 1 (Opening + Buy):** "Buy 100 shares of 600519." -> (Advice -> Open -> Trade -> BasicInfo -> Status/Verify -> Output)
**Example 2 (Closing):** "Sell TSLA." -> (Status -> ExecuteTrade(Sell) -> Status/Verify -> Output)
