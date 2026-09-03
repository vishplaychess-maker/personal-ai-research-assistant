---
name: commit-message
description: Generate conventional commit messages based on staged changes.
pinned: false
---
# Commit Message Generator

## When to use
When the user asks for a git commit message.

## Steps
1. Run git diff --cached.
2. Analyze changes.
3. Generate message in format: type(scope): subject