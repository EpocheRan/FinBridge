---
name: "manage-portfolio"
description: "Protocol for creating, modifying, and deleting investment portfolios. Use for portfolio lifecycle management and ensuring data consistency across asynchronous database updates."
---

# Manage Portfolio

## Scope

**Covers**
- Creating new investment portfolios with initial cash.
- Modifying portfolio metadata (name) or cash balances.
- Deleting portfolios and their associated history.
- Verification protocols for QuestDB's asynchronous WAL (Write-Ahead Log) commits.

**When to use**
- "Create a new portfolio named 'Growth Strategy' with 100k cash."
- "Rename portfolio 'A' to 'Conservative'."
- "Update the cash balance for portfolio ID 7d805a48."
- "Delete this portfolio and start over."

**When NOT to use**
- You need to manage specific stock holdings (use `manage-position`).
- You need to analyze market data or get trading advice (use standard market tools).
- You are performing read-only queries like "list portfolios" without intent to change (use `pm_list_portfolios` directly).

## Inputs

**Minimum required**
- `action`: create, modify, or delete.
- For create: `name`, `initial_cash`.
- For modify/delete: `portfolio_id`.
- For modify: `name` or `cash` (at least one).

**Missing-info strategy**
- If `portfolio_id` is missing for modify/delete, call `pm_list_portfolios` to find it by name.
- If `initial_cash` is missing for create, ask the user or default to 0.0 with a warning.

## Outputs (deliverables)

1) **Action Confirmation**: Clear statement of what tool was called and with what parameters.
2) **Verification Report**: Results of the post-operation check (status after update).
3) **Next Steps**: Advice on what the user might want to do next (e.g., "Add a position").

## Workflow (3 steps)

### 1) Pre-check and Preparation
- **Actions**: 
    - For **Create**: Call `pm_list_portfolios` to ensure the name isn't a duplicate.
    - For **Modify/Delete**: Ensure the `portfolio_id` exists and is correct.
- **Checks**: If a duplicate name is found, notify the user before proceeding.

### 2) Execute State Change
- **Actions**: Call the corresponding tool:
    - `pm_create_portfolio(name, initial_cash)`
    - `pm_modify_portfolio(portfolio_id, name, cash)`
    - `pm_delete_portfolio(portfolio_id)`
- **Checks**: Verify the tool returns `success: true`. If false, stop and report the error.
- **STOP**: You **MUST** stop here and wait for the tool output before proceeding to any verification or further calls.

### 3) Verify Consistency (CRITICAL)
- **Actions**: 
    - **Wait for Turn**: **DO NOT** call verification in the same turn as the modification/creation.
    - **Query**: In a **NEW response** (next turn), call `pm_list_portfolios` or `pm_get_portfolio_status`.
    - **Compare**: Ensure the live data matches the intended state.
- **Checks**: If data still doesn't match, the system delay might be higher; notify the user.

## Quality gate (required)
- Never assume a creation or modification was successful without a follow-up "read" call.
- Check `success`; on `success=false` read `error_details.type` / `suggestion` and fix the input.
- Always provide the `portfolio_id` in the final confirmation.

## Examples

**Example 1 (Create):** "Create a portfolio 'Alpha' with 200,000." -> (List -> Create -> List/Verify -> Output)
**Example 2 (Modify):** "Change the name of 7d805a48 to 'Beta'." -> (Modify -> Status/Verify -> Output)
