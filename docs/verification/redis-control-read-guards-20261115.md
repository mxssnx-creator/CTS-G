# Shared Redis guards for automatic service recovery and install preflight

Target: Stable-02-01 8049e8aba56969af41785021a12c0020567a9160. The #192
engine reader rejected Redis errors, but the HTTP sidecar and installer still
accepted nonempty error messages as credentials. The sidecar could consequently
attempt service recovery without valid keys, or persist a failed HGETALL reply
as settings. This is a further current-target repair of #19, not an old branch
merge or a complete instance-isolation port.

A pure shared module now owns the existing bounded read and error-reply rules.
The engine reuses it without changing its deadlines or value interpretation.
The sidecar uses it for settings and recovery; failed settings reads return
before creating a cache file. Recovery still honors STOP markers, disabled,
masked and transitioning units, and its existing retry pacing. Both credential
fields must succeed on the exact connection key. The Linux installer's predicate
uses the same reader through a silent CLI: only an exit status crosses back to
the shell, never a credential value. Missing Python/module/CLI fails closed;
there is no unguarded compatibility fallback. At most two six-second reads are
made for a credential pair. The install copy's syntax check includes the new
module, and the engine restore patch and both blob pins were regenerated.

The exact final 15-test fixture was run against a detached old target and failed
nine subcases. All 15 pass on the repaired tree. They exercise a real sidecar
recovery cycle with fake service and Redis calls, typed settings, and the real
installer predicate with a private fake CLI executable. No Redis server, systemd
manager or venue is contacted. Existing five engine-read tests also pass.
Full project, build, typecheck and exact-head CI outcomes are recorded separately
in the overall report; initial failed tests remain preserved.

Shared Pulse paths, Redis namespaces, unit names, log retention, backups and the
remaining #19 claims are still open. No installation, service restart, account
write, funded sample, or positive execution-PF acceptance is claimed.
