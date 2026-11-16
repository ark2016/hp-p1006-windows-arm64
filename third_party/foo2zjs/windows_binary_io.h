/* SPDX-License-Identifier: GPL-2.0-or-later
 * Local Windows portability shim: preserve every PBM, XQX and firmware byte.
 */
#ifndef P1006_WINDOWS_BINARY_IO_H
#define P1006_WINDOWS_BINARY_IO_H

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

static void init_binary_stdio(void)
{
#ifdef _WIN32
    if (_setmode(_fileno(stdin), _O_BINARY) == -1 ||
        _setmode(_fileno(stdout), _O_BINARY) == -1)
    {
        perror("Cannot enable binary standard streams");
        exit(1);
    }
#endif
}

#endif
