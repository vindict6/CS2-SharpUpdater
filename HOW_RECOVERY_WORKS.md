# How the Recovery Works

This is the part of the project I actually care about, so it gets its own writeup. Everything else—the depot fetching, the build, the release plumbing—is just glue code.

The interesting question is: when Valve updates the game and every signature in gamedata.json breaks, how do you find those functions again in a stripped binary without spending a whole day manually digging through a disassembler?

## The Problem

CounterStrikeSharp hooks functions inside libserver.so that aren't exported. These include things like ClientPrint, Host_Say, and CCSGameRules::TerminateRound.

Because the server binary is stripped, there is no symbol table to tell us where functions are located. We only have the basic dynamic symbols the linker needs, plus some basic type info (RTTI). You can't just look up ClientPrint by name; you have to find it based on what it looks like.

The standard solution is using byte-pattern signatures. You take a slice of the function's machine code near the beginning, wildcard the parts that change between updates, and end up with something like this:

```
55 48 8D 05 ? ? ? ? 48 89 E5 41 57 4D 89 CF 41 56
```

If that pattern is unique, the tool scans memory at startup, finds the match, and hooks the function.

Offsets work differently. They are numbers that point to a specific slot in a class's virtual function table (vtable).

Both methods break when the game updates because the compiler rearranges everything. It changes how registers are used, decides to inline code differently, and shifts functions around. The underlying logic of the function stays the same, but the exact bytes change, causing the signature to fail and offsets to point to the wrong slots.

## The Core Idea

An update changes a function's exact bytes, but it doesn't change what the function actually does.

It still uses the same text strings.

It still calls the same other functions.

Its internal structure has roughly the same shape.

If we can analyze these traits in the old binary (where the old signatures still work), we can search the new binary for a function with those exact same traits.

## Step Zero: Finding the Starting Point

We start with what we know: the old binary. The signatures in gamedata.json were written for the previous version of the game, so they still work there.

First, the tool runs the old signatures against the old binary to find the exact memory address of every function we need. Once we have those old locations, we can analyze how the functions are written and look for them in the new update.

## Strings: The Reliable Clue

If a function uses a specific text string, it is usually easy to find because text strings rarely change during an update. If the source code says "TerminateRound: unknown round end ID %i\n", the compiler places those exact text bytes into the data section (.rodata). The function code just references that location. The address of the text might change, but the text itself remains identical.

The String Matching Process:

Analyze the old function: The tool looks through the old assembly code for instructions that load text strings. (A quick technical note: The tool handles x86-64 relative addressing to calculate exactly where these strings live in memory and reads them.)

Catalog the strings: For Host_Say, the tool finds strings like "say" and "say_team".

Scan the new binary: The tool looks for those exact text bytes in the new data section to find their new addresses. Then, it searches the code section (.text) to find which functions are loading those new addresses.

Count the matches: The tool counts how many of these target strings appear in each new function. The function that uses the same group of strings is almost always the correct match.

Two rules about strings:

A single string isn't always enough. Common text logs appear in hundreds of functions. It is the specific combination of strings that matters. Because of this, the tool only uses string counts to narrow down the options, leaving the structural check to make the final decision.

Many functions do not use text strings at all. We need a backup plan for those.

## Structural Fingerprints: The Main Workhorse

For every function, the tool builds a simple structural profile. It counts:

The number of code blocks and jump targets.

Total instructions and function calls.

Overall size in bytes.

Instruction types: An exact count of how many moves (mov), loads (lea), calls (call), and conditional jumps the function uses.

To compare two functions, the tool uses a mathematical formula to score how similar their instruction counts are on a scale from 0 to 1. A score close to 1 means the functions use a nearly identical mix of instructions.

This works because the types of operations a function performs stay mostly identical after a recompile, even if the compiler swaps registers around or adjusts the layout. In practice, real matches score at 0.99 or higher. The tool also applies penalties if the overall size or block counts differ significantly.

## Testing the Method

To prove this works, I tested it on a few thousand functions that keep their names across updates (like standard library utilities).

For large functions (16+ blocks): The tool picked the correct match on the first try about 95% of the time, and placed it in the top five in every case I tested.

For tiny functions (5 blocks or fewer): It was highly unreliable (around a 20% success rate). Small functions often look identical to other basic helper functions, making them impossible to tell apart by structure alone.

Thankfully, the major functions we need to hook (like ClientPrint) are large and complex, meaning this structural check is highly accurate where it matters most.

## Call Graphs: Finding Silent Functions

For small functions that have no text strings and look too generic to identify by structure, we have to look at who they talk to. A great example is CEntityInstance::AcceptInput. It is small and looks like a hundred other basic functions.

However, its relationships are unique: it always calls one specific large helper function and then exits through a dispatch routine. This relationship generally survives a recompile, unless the compiler decides to inline the helper away.

Instead of searching for the small function directly, the tool searches for the large helper function it calls:

The large helper function is big enough to find easily using its structural fingerprint.

Once the tool finds the helper in the new binary, it looks for every piece of code that calls it.

It reviews that list of callers and selects the one that structurally matches the old version of our small function.

## Recovering Table Offsets

Signatures find functions; offsets find slots inside a virtual function table (vtable). If an update adds or removes a virtual function, all the slots below it shift, causing our old offset to point to the wrong function.

To fix this, we use the binary's built-in type information (RTTI). Since Linux uses a standard, predictable layout for objects, we can navigate from a class name directly to its function table:

Find the class name: The tool searches for the raw class name string (like 19CCSPlayerController).

Find the type block: It looks for the pointer that references that name string, which reveals the class's type information block.

Find the table: It finds the pointer referencing that type block. This leads directly to the primary function table, where the actual list of function pointers begins.

Once the tool has the table, it reads the old slot, identifies what function used to live there using our structural matching tool, and finds where that same function sits in the new table. The difference tells us exactly how much the offset shifted.

Not every offset in gamedata is a vtable slot, though. Some are struct field offsets, which this method can't recover. The tool spots those (the slot number is larger than the table actually has) and flags them for a human instead of guessing.

## Writing the New Signature

Once a function is found, generating a new signature is straightforward. The tool reads the function's assembly code from the beginning. It keeps the core instruction bytes but wildcards any data that changes based on memory location (like relative addresses and jump targets).

After processing each instruction, it checks if the resulting signature is completely unique within the new binary. The moment it becomes unique, the tool adds one more instruction for safety and stops.

If two different functions happen to have identical code bodies, the tool recognizes the ambiguity and refuses to output a guess.

## Common Traps

A few minor details caused significant issues during development:

Distinguishing code from padding: Compilers separate functions using padding bytes (0xCC). The tool looks for these bytes to find where the next function begins. However, 0xCC can also be part of a legitimate instruction. A naive detector can mistake a normal instruction for padding and start reading a function from the wrong spot. The fix was requiring at least two consecutive padding bytes: real padding runs are almost always more than one byte, whereas a 0xCC that's part of an instruction is essentially never doubled up.

Strict verification: This tool never outputs a guess it cannot verify. Every new signature is automatically tested against the new binary to ensure it hits exactly one address. If the confidence score is too low, the tool leaves it blank for a human to review. A missing signature simply turns off a feature; a wrong signature crashes the server.

## Final Verification

This entire process happens without running the game. While a unique match and a high structural score are excellent evidence, the ultimate test is a running server.

The output of this tool should be treated as a test build. We load it onto a temporary server, check the logs for errors, test the functions in-game, and only deploy it once verified. The tool changes the workflow from "spending a whole day manually reversing code after an update" to "running a quick sanity check on an automated build."
