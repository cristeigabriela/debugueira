// Managed target for debugueira's .NET tests: one method with locals, in three modes.
//
// No argument computes once and exits 0, `--throw` dies of an unhandled exception thrown
// inside Compute, and `--loop` prints its pid and keeps calling Compute for attach tests.

using System;
using System.Runtime.CompilerServices;
using System.Threading;

namespace HelloNet;

internal static class Program
{
    private static int Main(string[] args)
    {
        Console.WriteLine("hello from dotnet");

        string mode = args.Length > 0 ? args[0] : "";
        switch (mode)
        {
            case "":
                Compute(21);
                return 0;
            case "--throw":
                // A negative input is what makes Compute throw.
                Compute(-1);
                return 0;
            case "--loop":
                Console.WriteLine($"pid={Environment.ProcessId}");
                for (;;)
                {
                    Compute(21);
                    Thread.Sleep(100);
                }
            default:
                Console.Error.WriteLine("usage: HelloNet [--throw | --loop]");
                return 2;
        }
    }

    // Never inlined, so Compute always has its own frame to break in and inspect.
    [MethodImpl(MethodImplOptions.NoInlining)]
    private static int Compute(int x)
    {
        int doubled = x * 2;
        if (x < 0)
        {
            throw new InvalidOperationException("boom from Compute");
        }
        int result = doubled + 1;
        return result;
    }
}
