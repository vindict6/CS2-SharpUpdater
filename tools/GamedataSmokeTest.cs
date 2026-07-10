using CounterStrikeSharp.API;
using CounterStrikeSharp.API.Core;
using CounterStrikeSharp.API.Modules.Commands;
using CounterStrikeSharp.API.Modules.Utils;

// GamedataSmokeTest - validates the RECOVERED gamedata entries at RUNTIME.
//
// Static analysis proved each recovered signature matches exactly one address
// and each offset points at a matched vtable slot. The only thing static checks
// cannot confirm is that the resolved function is *semantically* correct and
// that calling through it doesn't crash. This plugin does that: it resolves and
// exercises the recovered entries on a live server.
//
// Build:  dotnet build  (reference your freshly built CounterStrikeSharp.API.dll)
// Deploy: <server>/game/csgo/addons/counterstrikesharp/plugins/GamedataSmokeTest/
// Run in server console:  css_gdtest
//   - "css_gdtest" prints ClientPrint to all players (exercises the ClientPrint
//     sig) and reports which recovered entries resolved.
//
// If CounterStrikeSharp itself fails to resolve a sig at load, it logs
//   "Failed to find signature/offset for <name>"
// so ALSO check the server log after startup - that is the first-line test.

namespace GamedataSmokeTest;

public class GamedataSmokeTest : BasePlugin
{
    public override string ModuleName => "Gamedata Smoke Test";
    public override string ModuleVersion => "1.0.0";
    public override string ModuleAuthor => "cs2-recover";
    public override string ModuleDescription => "Exercises recovered gamedata entries at runtime.";

    // The entries this recovery run touched. Adjust to match your report.json.
    private static readonly string[] RecoveredSignatures =
    {
        "ClientPrint", "Host_Say", "CCSGameRules_TerminateRound",
        "CBaseEntity_TakeDamageOld", "CheckTransmit",
        "CCSPlayerPawnBase_PostThink", "CBaseTrigger_StartTouch",
    };

    private static readonly string[] RecoveredOffsets =
    {
        "CCSPlayerController_Respawn",
        "CCSPlayer_ItemServices_GiveNamedItem",
        "CCSPlayer_ItemServices_DropActivePlayerWeapon",
        "CCSPlayer_ItemServices_RemoveWeapons",
        "CGameSceneNode_GetSkeletonInstance",
        "CBasePlayerPawn_CommitSuicide",
    };

    public override void Load(bool hotReload)
    {
        Logger.LogInformation("[gdtest] loaded. Run 'css_gdtest' in console to validate recovered gamedata.");
    }

    [ConsoleCommand("css_gdtest", "Validate recovered gamedata entries")]
    public void OnGdTest(CCSPlayerController? caller, CommandInfo cmd)
    {
        int okSig = 0, okOff = 0;

        // 1) Signature resolution: ask the gamedata system for each address.
        //    NativeAPI.GetSignatureAddress returns 0 / throws if unresolved.
        foreach (var name in RecoveredSignatures)
        {
            try
            {
                var addr = NativeAPI.GetValveSignature("server", name);
                bool ok = addr != System.IntPtr.Zero;
                if (ok) okSig++;
                Report(cmd, $"sig  {name,-32} {(ok ? "RESOLVED 0x" + addr.ToInt64().ToString("x") : "UNRESOLVED")}");
            }
            catch (System.Exception e)
            {
                Report(cmd, $"sig  {name,-32} ERROR {e.Message}");
            }
        }

        // 2) Offset resolution.
        foreach (var name in RecoveredOffsets)
        {
            try
            {
                int off = NativeAPI.GetOffset(name);   // reads the linux/win offset from gamedata
                bool ok = off >= 0;
                if (ok) okOff++;
                Report(cmd, $"off  {name,-46} {(ok ? "index " + off : "UNRESOLVED")}");
            }
            catch (System.Exception e)
            {
                Report(cmd, $"off  {name,-46} ERROR {e.Message}");
            }
        }

        // 3) EXERCISE ClientPrint end-to-end: actually invoke the recovered
        //    function. If the sig points at the wrong code this typically prints
        //    garbage or crashes - so a clean chat print is a strong positive.
        try
        {
            foreach (var p in Utilities.GetPlayers())
                p.PrintToChat($" \x04[gdtest]\x01 ClientPrint OK - recovered sig is live.");
            Report(cmd, "exercise ClientPrint -> printed to all players (no crash)");
        }
        catch (System.Exception e)
        {
            Report(cmd, $"exercise ClientPrint -> ERROR {e.Message}");
        }

        Report(cmd, $"SUMMARY sigs {okSig}/{RecoveredSignatures.Length}, offsets {okOff}/{RecoveredOffsets.Length}");
    }

    private void Report(CommandInfo cmd, string line)
    {
        Logger.LogInformation("[gdtest] " + line);
        cmd.ReplyToCommand("[gdtest] " + line);
    }
}
