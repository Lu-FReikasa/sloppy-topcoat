//go:build !unix

package store

func ownerOf(string) (uid, gid int, ok bool) { return 0, 0, false }

func giveTo(string, int, int) error { return nil }
