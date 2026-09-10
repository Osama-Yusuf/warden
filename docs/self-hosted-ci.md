# Running CI on a local runner

This documents the maintainer's own runner setup. It's useful when GitHub
Actions minutes are metered (a private repo, or a public one that has run past
its allowance): when they run out every workflow fails in ~1 second with zero
steps, which reads like a broken build but is really the `$0` default spending
limit rejecting the job. Confirm with `gh run view <id> --json jobs` and look
for `steps: 0`.

Two ways out. The clean one is to raise the limit at
<https://github.com/settings/billing/spending_limit>. The free one is the local
Docker runner fleet below.

## The fleet

```
scripts/runner-up            # build the image if needed, mint a token, start, wait for online
scripts/runner-up --rebuild  # force an image rebuild first
scripts/runner-down          # stop and deregister
docker logs -f warden-runner-1
```

`runs-on` in `test.yml` is the repo variable `RUNNER_LABEL`, so switching where
jobs run is not a commit:

```
gh variable set RUNNER_LABEL --body self-hosted --repo Osama-Yusuf/warden   # ours
gh variable delete RUNNER_LABEL --repo Osama-Yusuf/warden                   # back to GitHub's
```

The variable alone is not enough for a PR: `pull_request` runs use the workflow
file from the PR's own branch, so a branch cut before this landed still says
`ubuntu-latest`. Rebase on main first.

## Notes

- Sizing is in `docker/runner/limits.env` (1 runner, 4 CPUs, 4 GB). The host
  usually runs other work alongside CI, so the footprint is kept small.
- The runner mounts the host Docker socket and uses host networking, so the
  integration job's DB service containers are siblings on the host, reachable at
  localhost. Their host ports are shifted (55432/53306/57017/56379) so they do
  not collide with the dev containers already on the standard ports.
- `build.yml` (release wheels + desktop binaries) stays on GitHub's runners: it
  only runs on tags and its desktop matrix needs macOS and Windows.
- The registration token `runner-up` mints is repo-scoped and expires within the
  hour; it is passed as an env var and never written to disk. Nothing in the
  repo is a credential.
