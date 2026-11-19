# Workflows Directory

This directory contains Standard Operating Procedures (SOPs) written in Markdown that define what to do and how to do it.

## Purpose

Each workflow:
- Defines a specific objective or task
- Lists required inputs and expected outputs
- Specifies which tools to use and in what sequence
- Documents edge cases and error handling
- Improves over time as we learn

## Workflow Template

Create workflows following this structure:

```markdown
# Workflow: [Task Name]

## Objective
Brief description of what this workflow accomplishes.

## Inputs
- Input 1: description
- Input 2: description

## Outputs
- Output 1: description
- Output 2: description

## Steps
1. First step and tool(s) to use
2. Second step and tool(s) to use
3. Continue...

## Error Handling
- If error X occurs, do Y
- If error Z occurs, do W

## Notes and Constraints
- API rate limits
- Timing considerations
- Known issues
```

## Naming Convention

Workflow files should be named descriptively: `workflow_name.md`

Examples:
- `scrape_competitor_data.md`
- `generate_market_report.md`
- `sync_data_to_sheets.md`

## Improvements

When you:
- Discover a better method
- Hit an error and find a workaround
- Learn about API constraints

**Update the workflow** so the next time it's clearer and more reliable.
