import { CircleAlert } from "lucide-react";
import { type FormEvent, useState } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router";

import { ApiError } from "@/api/client";
import { useAuth } from "@/auth/AuthProvider";
import { Button } from "@/components/ui/button";
import { FieldError, Input, Label } from "@/components/ui/form";

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** Only same-app paths: `?next=https://evil.example` must not become an open redirect. */
function safeNext(raw: string | null): string {
  return raw && raw.startsWith("/") && !raw.startsWith("//") ? raw : "/";
}

export function LoginPage() {
  const { status, login, expired } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const next = safeNext(params.get("next"));

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [touched, setTouched] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (status === "authenticated") return <Navigate to={next} replace />;

  const emailError = touched && !EMAIL.test(email) ? "Enter an email address." : null;
  const passwordError = touched && !password ? "Enter your password." : null;

  async function submit(event: FormEvent) {
    event.preventDefault();
    setTouched(true);
    if (!EMAIL.test(email) || !password) return;
    setBusy(true);
    setError(null);
    try {
      await login(email.trim(), password);
      navigate(next, { replace: true });
    } catch (cause) {
      if (cause instanceof ApiError && cause.code === "rate_limited") {
        setError("Too many attempts. Wait a minute and try again.");
      } else if (cause instanceof ApiError && cause.status === 401) {
        // The API gives one message for every cause; so do we.
        setError("Email or password is incorrect.");
      } else {
        setError(cause instanceof Error ? cause.message : "Sign-in failed.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid min-h-full place-items-center bg-page px-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center gap-2.5">
          <img src="/favicon.svg" alt="" className="size-8" />
          <div>
            <h1 className="text-lg font-semibold tracking-tight">Concordance</h1>
            <p className="text-xs text-muted">Provider sanctions &amp; exclusions reconciliation</p>
          </div>
        </div>
        <form onSubmit={submit} noValidate className="space-y-4 rounded-lg bg-surface p-6 ring-1 ring-border">
          {expired ? (
            <p className="rounded-md bg-warning/15 px-3 py-2 text-sm text-ink ring-1 ring-warning/40">
              Your session ended. Sign in again to continue where you were.
            </p>
          ) : null}
          <div className="space-y-1.5">
            <Label htmlFor="email">Email</Label>
            <Input
              id="email"
              type="email"
              autoComplete="username"
              autoFocus
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              aria-invalid={Boolean(emailError)}
              aria-describedby={emailError ? "email-error" : undefined}
            />
            <FieldError id="email-error">{emailError}</FieldError>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="password">Password</Label>
            <Input
              id="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              aria-invalid={Boolean(passwordError)}
              aria-describedby={passwordError ? "password-error" : undefined}
            />
            <FieldError id="password-error">{passwordError}</FieldError>
          </div>
          {error ? (
            <p role="alert" className="flex items-center gap-2 text-sm text-critical-text">
              <CircleAlert className="size-4" /> {error}
            </p>
          ) : null}
          <Button type="submit" variant="primary" className="w-full" size="lg" loading={busy}>
            Sign in
          </Button>
        </form>
      </div>
    </div>
  );
}
