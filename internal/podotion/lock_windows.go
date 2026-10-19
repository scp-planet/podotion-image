//go:build windows

package podotion

import (
	"os"
	"syscall"
	"unsafe"
)

var (
	kernel32         = syscall.NewLazyDLL("kernel32.dll")
	procLockFileEx   = kernel32.NewProc("LockFileEx")
	procUnlockFileEx = kernel32.NewProc("UnlockFileEx")
)

type windowsFileLock struct {
	handle     *os.File
	overlapped syscall.Overlapped
}

func lockHandle(handle *os.File) (fileLock, bool, error) {
	var overlapped syscall.Overlapped
	const lockfileFailImmediately = 0x00000001
	const lockfileExclusiveLock = 0x00000002
	result, _, callErr := procLockFileEx.Call(
		uintptr(syscall.Handle(handle.Fd())),
		uintptr(lockfileFailImmediately|lockfileExclusiveLock),
		0,
		1,
		0,
		uintptr(unsafe.Pointer(&overlapped)),
	)
	if result == 0 {
		if callErr == syscall.Errno(33) || callErr == syscall.Errno(997) {
			return nil, false, nil
		}
		return nil, false, callErr
	}
	return &windowsFileLock{handle: handle, overlapped: overlapped}, true, nil
}

func (l *windowsFileLock) Release() error {
	_, _, _ = procUnlockFileEx.Call(
		uintptr(syscall.Handle(l.handle.Fd())),
		0,
		1,
		0,
		uintptr(unsafe.Pointer(&l.overlapped)),
	)
	return l.handle.Close()
}
