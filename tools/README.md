# Tools Directory

This directory contains Python scripts that execute deterministic operations in the WAT framework.

## Purpose

Each script in this directory:
- Performs a specific, testable operation
- Uses credentials from `.env`
- Is idempotent where possible
- Handles errors gracefully with clear error messages
- Logs important operations for debugging

## Structure

Each tool should:
1. Accept clear command-line arguments
2. Load environment variables from `.env`
3. Perform the operation
4. Return structured output (JSON when possible)
5. Exit with appropriate status codes

## Example

```python
# tools/example_tool.py
import os
from dotenv import load_dotenv

load_dotenv()

def main():
    api_key = os.getenv("API_KEY")
    # Perform operation
    print("Operation completed")

if __name__ == "__main__":
    main()
```

## Testing

Always test tools with:
```bash
python tools/your_tool.py --test
```

Before adding paid API calls, check with the project owner.
