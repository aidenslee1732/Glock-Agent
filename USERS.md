# Glock User Guide

Glock is an AI-powered coding assistant that runs in your terminal.

---

## Installation

```bash
curl -fsSL getglock.dev/install.sh | bash
```

That's it. Glock is now installed.

---

## Getting Started

### 1. Login

```bash
glock login
```

This opens your browser to authenticate. Once complete, you're ready to use Glock.

### 2. Start Coding

```bash
cd your-project
glock
```

This starts an interactive session where you can ask Glock to help with your code.

### 3. Logout (when needed)

```bash
glock logout
```

---

## Commands

| Command | Description |
|---------|-------------|
| `glock login` | Login via browser |
| `glock` | Start interactive session |
| `glock logout` | Logout |
| `glock version` | Show version |

---

## Using Glock

Once you start a session with `glock`, you can ask it to:

**Read and explain code:**
```
> What does the UserService class do?
> Explain the authentication flow
```

**Edit code:**
```
> Fix the bug in the login function
> Add input validation to the registration form
```

**Run commands:**
```
> Run the tests
> Install axios
```

**Search:**
```
> Find all TODO comments
> Where is validateEmail used?
```

### Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Enter` | Send message |
| `Ctrl+C` | Cancel current operation |
| `Ctrl+D` | Exit session |

---

## Tips

- **Be specific**: "Fix the auth bug in login.py" works better than "fix the bug"
- **Review changes**: Always review what Glock proposes before accepting
- **Your code stays local**: Tools run on your machine, code is never uploaded

---

## Troubleshooting

### "Command not found: glock"

Restart your terminal, or run:
```bash
source ~/.bashrc  # or ~/.zshrc
```

### "Authentication failed"

```bash
glock logout
glock login
```

### Connection issues

Check your internet connection and try again.

---

## Support

- **Documentation**: https://docs.glock.dev
- **Discord**: https://discord.gg/glock
- **Email**: support@glock.dev
