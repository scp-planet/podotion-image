//go:build !windows

package podotion

import (
	"os"
	"syscall"
)

type unixFileLock struct {
	handle *os.File
}

func lockHandle(handle *os.File) (fileLock, bool, error) {
	err := syscall.Flock(int(handle.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
	if err != nil {
		if err == syscall.EWOULDBLOCK || err == syscall.EAGAIN {
			return nil, false, nil
		}
		return nil, false, err
	}
	return &unixFileLock{handle: handle}, true, nil
}

func (l *unixFileLock) Release() error {
	err := syscall.Flock(int(l.handle.Fd()), syscall.LOCK_UN)
	closeErr := l.handle.Close()
	if err != nil {
		return err
	}
	return closeErr
}
