import { useQueryClient } from "@tanstack/react-query";
import { createContext, type ReactNode, use, useCallback, useEffect, useMemo, useState } from "react";

import * as client from "@/api/client";
import type { User } from "@/api/types";

type Status = "restoring" | "authenticated" | "anonymous";

interface AuthState {
  status: Status;
  user: User | null;
  /** True once a live session ended under the user (refresh refused). */
  expired: boolean;
  login: (email: string, password: string) => Promise<User>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<Status>("restoring");
  const [user, setUser] = useState<User | null>(null);
  const [expired, setExpired] = useState(false);

  // On boot, trade the refresh cookie (if any) for a session.
  useEffect(() => {
    let cancelled = false;
    void client.restoreSession().then((restored) => {
      if (cancelled) return;
      setUser(restored);
      setStatus(restored ? "authenticated" : "anonymous");
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(
    () =>
      client.onSessionEvent(() => {
        setExpired(true);
        setUser(null);
        setStatus("anonymous");
        queryClient.clear();
      }),
    [queryClient],
  );

  const login = useCallback(async (email: string, password: string) => {
    const signedIn = await client.login(email, password);
    setUser(signedIn);
    setExpired(false);
    setStatus("authenticated");
    return signedIn;
  }, []);

  const logout = useCallback(async () => {
    await client.logout();
    // Nothing one user fetched may be shown to the next.
    queryClient.clear();
    setUser(null);
    setStatus("anonymous");
  }, [queryClient]);

  const value = useMemo(() => ({ status, user, expired, login, logout }), [status, user, expired, login, logout]);
  return <AuthContext value={value}>{children}</AuthContext>;
}

export function useAuth(): AuthState {
  const context = use(AuthContext);
  if (!context) throw new Error("useAuth outside <AuthProvider>");
  return context;
}

/** The signed-in user. Only valid beneath <RequireAuth>. */
export function useUser(): User {
  const { user } = useAuth();
  if (!user) throw new Error("useUser outside an authenticated route");
  return user;
}

export function useIsAdmin(): boolean {
  return useAuth().user?.role === "admin";
}
