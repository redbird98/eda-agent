# eda-agent bridge for EasyEDA Pro

The editor half of the EasyEDA backend. Without it the Python side
listens and nothing ever connects.

## Why there are two halves

Altium is driven from outside: the server writes request files and
Altium's polling loop picks them up. **EasyEDA works the other way
round.** Its extension API runs inside the editor and reaches out
(`SYS_WebSocket.register`), so the server listens and the editor dials
it. Nothing in the Python process can start EasyEDA or make it connect.

That is why installing this is not optional, and why every tool reports
the source as unreachable until it is running.

## Install

```bash
python extensions/easyeda/build.py
```

Then in EasyEDA Pro: **Settings > Extensions**, install from this
folder.

The build is a copy. `main.js` is a single ES module with no imports, so
there is nothing to bundle, and `build.py` exists to check that rather
than to pretend at a toolchain: if the source ever grows an import it
refuses, because a copy would then produce an entry point with
unresolved dependencies that fails at load time instead of build time.

`dist/` is not committed, the same way the built Altium script is not.

## Connecting

The extension connects on load. The **eda-agent** menu on the PCB and
schematic pages also offers **Connect** and **Disconnect**, which is
what you want after restarting the server.

**No port needs configuring.** The extension scans 49620-49629 (the
range EasyEDA's own bridge server uses), reads `GET /health` from each,
and connects only to one whose `service` is `eda-agent-bridge`. That
check matters: without it, a WebSocket handshake would be sent to
whatever happened to answer the port.

**Nothing here requires a host global.** EasyEDA's guidance is that
standard browser APIs are not available to an extension's main process,
so `fetch` and the host timers are both preferences with fallbacks, and
`SYS_Timer` is used when offered. A test loads the extension on a
runtime with none of the three and requires it to survive.

That is not the same worry as failing to connect. The retry loop was
armed with a bare `setInterval` inside `connect()`, which `activate()`
calls at load, so a missing global threw while the module was
initialising: no menu item, no visible error, and no way to tell it
apart from never having installed the extension.

One consequence worth knowing: the fallback **cannot tell two eda-agent
servers apart**. With no `fetch` there is no `/health` to ask, so it
keeps whichever port accepts first. Run one server, or set
`EDA_AGENT_EASYEDA_PORT` and the extension's `serverUrl` so there is
nothing to choose between.

**Discovery does not require `fetch`.** EasyEDA's own guidance is that
standard browser APIs are not available to an extension's main process,
so the `/health` probe is a preference rather than a dependency: when
`fetch` is missing, the extension opens a WebSocket to each port in the
range and keeps whichever connects, closing the others. That matters
because a discovery step resting on `fetch` alone fails on every port
and reports "no server found", which is the same message as the server
being down: the extension looks correct and never connects.

It then retries every few seconds until it finds a server. That retry is
the difference between working and never connecting, because
`SYS_WebSocket.register()` fails silently when nothing is listening at
that instant and never tries again. With it, starting the server and the
editor in either order works.

`EDA_AGENT_EASYEDA_HOST` and `EDA_AGENT_EASYEDA_PORT` still pin the
server side when a firewall rule needs a fixed port.

The server binds to loopback. It is a command channel that executes
edits, and it is not hardened for a hostile network.

## Protocol

The server sends `{id, command, params}`; the extension answers
`{id, result}` or `{id, error}`. Requests are correlated by id, so a
slow reply cannot be mistaken for the answer to a later question.

The envelope is built in exactly one place in `main.js`, so a new
command cannot invent its own reply shape. A test enforces that, along
with the command names matching what Python sends, the `registerFn`
values matching real exports, and the manifest's `entry` matching what
the build writes. Each of those is a fact stated in two files with
nothing else connecting them.

## What is verified, and what is not

Every EasyEDA API name here comes from their published reference rather
than recollection, including the instance naming: class `PCB_Drc` is
reached as `eda.pcb_Drc`, first three letters lowercased. Getting that
wrong yields `undefined` rather than an error, so it fails as a
confusing null far from the cause.

Three things are checked mechanically, each because it failed once:

- **Every `eda.*` call is a documented method.** Checked by executing all
  the handlers against a recording proxy, not by reading the source, so
  a call inside a branch cannot hide.
- **Every call passes the arguments its signature requires.** The
  existence check cannot see arity, and two handlers shipped calling a
  three- and a six-parameter method with one argument.
- **Every parameter the Python side sends is one a handler reads.** A
  wrong command name fails loudly; a wrong parameter name does not, it
  just takes the default and reports success.

**None of this has run inside EasyEDA Pro.** The transport is tested
against a fake editor over real sockets, and the framing against RFC
6455's own worked example, but the command vocabulary has never
round-tripped against a live editor. Both halves report
`verified_live: false` for that reason. A clean load is the first test,
not confirmation.

## Layers are named, never numbered

Commands that place something take a layer name (`TOP`,
`TOP_SILKSCREEN`, `BOARD_OUTLINE`). EasyEDA's layer ids are a **numeric**
enum, and their guidance is to use the members rather than the values, so
the extension resolves the name against the runtime's own enum at call
time.

A number chosen on the Python side would be this project's copy of their
numbering, and it would go wrong quietly: the primitive lands on a
different layer instead of failing.

## The two canvases count differently

EasyEDA's **PCB** canvas is 1 unit = 1 mil. Its **schematic** canvas is
1 unit = 0.01 inch, which is **ten** mils. EasyEDA's own guidance calls
mixing the two the most common mistake made against this API.

It lasts because nothing errors. A schematic laid out in mils and sent
unconverted lands ten times too far out, which reads as a bad layout
rather than a bad unit.

Commands here carry whatever the Python side sent. The conversion is
done there, once, in `MILS_PER_SCHEMATIC_UNIT`, so every tool takes mils
and the rule has one home rather than one per call site.

## Destructive commands

`pcb.clear_routing`, `pcb.auto_route` and `pcb.delete_primitives` change
or remove work wholesale. All three refuse unless `confirm` is true, and
**both halves check independently**: the extension is reachable by
anything speaking this protocol, so it cannot assume a caller already
checked.
