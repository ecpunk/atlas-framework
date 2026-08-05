# If Claude stops connecting

This is what to do if Claude (or another assistant) suddenly can't reach
your Atlas store anymore — the thing that keeps track of your projects,
tasks, and notes.

## 1. Check the box is on and reachable

The Atlas server runs on a computer that has to be powered on and connected
to your network. If it's been turned off, unplugged, or moved, plug it back
in and give it a minute to boot.

## 2. Reset the connection code

If the box is on but the connection still fails, the login code it uses may
have gone stale or gotten lost. Fix it yourself:

1. Get a terminal open on the box (or SSH into it if you normally do that).
2. Run:
   ```
   ./recovery/reset-login-secret.sh
   ```
   from inside the Atlas folder.
3. It will print a new code and restart the server. **Write the code down —
   it is only shown once.**

## 3. Re-add the connector

Wherever you connect from (a phone app, a website, a desktop app), remove
the old Atlas connection and add it again, using the new code from step 2
when it asks you to log in.

## If none of that works

Something deeper is wrong (network, disk, or the server software itself).
That's a job for whoever set this up for you, or for an AI assistant with
access to the box — hand them this file and tell them what you tried.
