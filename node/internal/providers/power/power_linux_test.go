package power

import (
	"context"
	"errors"
	"strings"
	"testing"
)

func TestLinuxSleepRunsSystemctlSuspend(t *testing.T) {
	var got string
	s := system{run: func(_ context.Context, name string, args ...string) ([]byte, error) {
		got = name + " " + strings.Join(args, " ")
		return nil, nil
	}}
	if err := s.Sleep(context.Background()); err != nil || got != "systemctl suspend" {
		t.Fatalf("%q %v", got, err)
	}
	s.run = func(context.Context, string, ...string) ([]byte, error) {
		return []byte("Access denied"), errors.New("exit status 1")
	}
	if err := s.Sleep(context.Background()); err == nil || !strings.Contains(err.Error(), "Access denied") {
		t.Fatalf("the reason must be visible: %v", err)
	}
}
