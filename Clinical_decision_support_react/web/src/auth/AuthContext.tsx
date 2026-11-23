import { createContext, ReactNode, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, ApiError, onAuthError, request } from "../api/client";
import type { Action, Profile, SessionResponse, SessionState } from "../api/types";

interface AuthValue {
  state: SessionState | "loading" | "offline";
  user: Profile | null;
  notice: string;
  offlineError: string;
  setSession: (session: SessionResponse) => void;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
  can: (module: string, action?: Action) => boolean;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [state, setState] = useState<AuthValue["state"]>("loading");
  const [user, setUser] = useState<Profile | null>(null);
  const [notice, setNotice] = useState("");
  const [offlineError, setOfflineError] = useState("");

  const setSession = useCallback((session: SessionResponse) => {
    setState(session.state);
    setUser(session.user);
    if (session.notice) setNotice(session.notice);
    if (session.state === "ready") setNotice("");
  }, []);

  const refresh = useCallback(async () => {
    try {
      setSession(await request<SessionResponse>("/api/auth/session", { silentAuth: true }));
      setOfflineError("");
    } catch (error) {
      setState("offline");
      setOfflineError(error instanceof ApiError ? error.message : "Cannot reach the server.");
    }
  }, [setSession]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Any 401 from any screen (idle timeout, deactivated account, server restart) returns to sign-in.
  useEffect(
    () =>
      onAuthError((error) => {
        if (error.code === "password_change_required") {
          setState("password_change");
          return;
        }
        queryClient.clear(); // never show the previous user's data to the next one
        setUser(null);
        setState("signed_out");
        setNotice(error.message);
      }),
    [queryClient],
  );

  const logout = useCallback(async () => {
    try {
      await api.post("/api/auth/logout");
    } finally {
      queryClient.clear();
      setUser(null);
      setState("signed_out");
      setNotice("You signed out.");
    }
  }, [queryClient]);

  const can = useCallback((module: string, action: Action = "View") => Boolean(user?.permissions[module]?.[action]), [user]);

  const value = useMemo(
    () => ({ state, user, notice, offlineError, setSession, refresh, logout, can }),
    [state, user, notice, offlineError, setSession, refresh, logout, can],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside <AuthProvider>");
  return value;
}
