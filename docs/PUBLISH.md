# Publish to GitHub

The distribution ZIP contains a local Git repository with an initial `main`
commit and no remote. Publishing it does not install or modify the Pi.
Git and GitHub CLI must be installed on the Windows machine.

Extract the ZIP, sign in to GitHub as `yuan-leo`, and run these in PowerShell:

```powershell
Expand-Archive -LiteralPath "$HOME\Downloads\mobileusb-repo.zip" -DestinationPath "$HOME\Downloads\mobileusb-github"
```

```powershell
gh auth login --hostname github.com --git-protocol https --web
```

```powershell
gh auth setup-git --hostname github.com
```

```powershell
gh repo create yuan-leo/mobileusb --private --source "$HOME\Downloads\mobileusb-github\mobileusb" --remote origin --push
```

If the target repository already exists, creation stops. Do not force-push or
replace an existing repository without inspecting it first. Do not initialize
a README or license on GitHub separately before this command. GitHub CLI uses
your locally authenticated account; no access token needs to be pasted into chat.

Verify after publishing:

```powershell
gh repo view yuan-leo/mobileusb --json nameWithOwner,visibility,url,defaultBranchRef
```

Official command references:
- https://cli.github.com/manual/gh_repo_create
- https://cli.github.com/manual/gh_auth_login
- https://cli.github.com/manual/gh_auth_setup-git
