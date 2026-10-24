/*
 * Process-tree target: starts worker.exe 0..4 and side.exe, waits for all, reports exit codes.
 *
 * The children are found next to spawner.exe and share its console and std handles, so
 * child-debugging tests see six children (one of which crashes) and no stray windows.
 * Each child's line is printed only after every child exited, in launch order.
 */
#include <stdio.h>
#include <wchar.h>
#include <windows.h>

#define CHILD_COUNT 6

struct child {
    const wchar_t *image;
    // NULL means the child gets no argument.
    const wchar_t *arg;
};

static struct child children[CHILD_COUNT] = {
    {L"worker.exe", L"0"},
    {L"worker.exe", L"1"},
    {L"worker.exe", L"2"},
    {L"worker.exe", L"3"},
    {L"worker.exe", L"4"},
    {L"side.exe", NULL},
};

static HANDLE inheritable_std_handle(DWORD which)
{
    HANDLE h = GetStdHandle(which);

    // STARTF_USESTDHANDLES only works if the handles can be inherited; a console may hand them out without.
    if (h != NULL && h != INVALID_HANDLE_VALUE) {
        SetHandleInformation(h, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
    }
    return h;
}

int main(void)
{
    wchar_t dir[MAX_PATH];
    wchar_t *last_slash;
    STARTUPINFOW si;
    HANDLE processes[CHILD_COUNT];
    int i;

    if (GetModuleFileNameW(NULL, dir, MAX_PATH) == 0) {
        fprintf(stderr, "spawner: GetModuleFileNameW failed: %lu\n", GetLastError());
        return 1;
    }
    last_slash = wcsrchr(dir, L'\\');
    if (last_slash != NULL) {
        *last_slash = L'\0';
    }

    ZeroMemory(&si, sizeof si);
    si.cb = sizeof si;
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = inheritable_std_handle(STD_INPUT_HANDLE);
    si.hStdOutput = inheritable_std_handle(STD_OUTPUT_HANDLE);
    si.hStdError = inheritable_std_handle(STD_ERROR_HANDLE);

    for (i = 0; i < CHILD_COUNT; i++) {
        wchar_t path[MAX_PATH];
        wchar_t cmdline[2 * MAX_PATH];
        PROCESS_INFORMATION pi;

        swprintf(path, MAX_PATH, L"%ls\\%ls", dir, children[i].image);
        if (children[i].arg != NULL) {
            swprintf(cmdline, 2 * MAX_PATH, L"\"%ls\" %ls", path, children[i].arg);
        } else {
            swprintf(cmdline, 2 * MAX_PATH, L"\"%ls\"", path);
        }

        // Inherited handles and no CREATE_NEW_CONSOLE: children write to our stdout, not a window of their own.
        if (!CreateProcessW(path, cmdline, NULL, NULL, TRUE, 0, NULL, NULL, &si, &pi)) {
            fprintf(stderr, "spawner: failed to start %ls: %lu\n", path, GetLastError());
            return 1;
        }
        CloseHandle(pi.hThread);
        processes[i] = pi.hProcess;
    }

    WaitForMultipleObjects(CHILD_COUNT, processes, TRUE, INFINITE);

    for (i = 0; i < CHILD_COUNT; i++) {
        DWORD code = 0;

        GetExitCodeProcess(processes[i], &code);
        if (children[i].arg != NULL) {
            printf("child %ls %ls exit=0x%lX\n", children[i].image, children[i].arg, code);
        } else {
            printf("child %ls exit=0x%lX\n", children[i].image, code);
        }
        CloseHandle(processes[i]);
    }
    fflush(stdout);
    return 0;
}
