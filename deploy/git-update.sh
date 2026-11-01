#!/usr/bin/env bash
# Git checkout/update for the installer. A hard reset is explicit opt-in only.

cts_git_update() {
  local root="$1" remote="$2" branch="$3" force="$4"

  if [[ "$force" == "1" ]]; then
    git -C "$root" checkout -f "$branch" >/dev/null 2>&1 \
      || git -C "$root" checkout -B "$branch" "$remote/$branch" || return
    git -C "$root" reset --hard "$remote/$branch"
    return
  fi

  # A failed checkout must never become checkout -B: it may have failed because
  # local edits would be lost. Create a branch only when it does not yet exist.
  if git -C "$root" show-ref --verify --quiet "refs/heads/$branch"; then
    git -C "$root" checkout "$branch" || return
  else
    git -C "$root" checkout -b "$branch" --track "$remote/$branch" || return
  fi
  git -C "$root" merge --ff-only "$remote/$branch"
}
