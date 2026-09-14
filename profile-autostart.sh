# Optional: start the panel on every interactive root login (PVE web Shell,
# interactive SSH, tty). Append this to /root/.profile.
#
# It is deliberately guarded so it does NOT run for `ssh host "cmd"`, scp or
# rsync — those are neither login shells nor interactive, and breaking them
# would break any automation you have against the host. Verify after installing:
#   time ssh root@host 'echo ok'      # must return immediately
#
# Escapes: press any key during the countdown · NO_PANEL=1 · the `panel` command
if [[ $- == *i* ]] && [ -t 0 ] && [ -t 1 ] && [ -z "$NO_PANEL" ] && [ -z "$PANEL_ACTIVE" ]; then
    export PANEL_ACTIVE=1
    printf "\n  \033[1mProxmox Panel\033[0m starting in 3s… press any key for a shell.\n"
    if read -rsn1 -t 3 _; then
        printf "  → shell. (run \033[1mpanel\033[0m whenever you want it)\n\n"
    else
        /usr/local/bin/panel
    fi
fi
