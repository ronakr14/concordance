import {
  LayoutDashboard,
  ListChecks,
  LogOut,
  Monitor,
  Moon,
  ScrollText,
  ShieldAlert,
  Sun,
  Users,
  Briefcase,
} from "lucide-react";
import { NavLink, Outlet, useNavigate } from "react-router";

import { useAuth, useIsAdmin, useUser } from "@/auth/AuthProvider";
import { Button } from "@/components/ui/button";
import { MenuContent, MenuItem, MenuLabel, MenuRoot, MenuSeparator, MenuTrigger } from "@/components/ui/overlay";
import { useTheme } from "@/lib/theme";
import { cn } from "@/lib/utils";

const NAV = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard, end: true },
  { to: "/providers", label: "Providers", icon: Users },
  { to: "/sanctions", label: "Sanctions", icon: ShieldAlert },
  { to: "/queue", label: "Queue", icon: ListChecks },
  { to: "/cases", label: "Cases", icon: Briefcase },
  { to: "/audit", label: "Audit", icon: ScrollText, adminOnly: true },
] as const;

export function AppShell() {
  const isAdmin = useIsAdmin();
  return (
    <div className="grid h-full grid-cols-[13.5rem_1fr]">
      <aside className="flex flex-col border-r bg-surface">
        <div className="flex items-center gap-2 px-4 py-4">
          <img src="/favicon.svg" alt="" className="size-6" />
          <span className="text-[15px] font-semibold tracking-tight">Concordance</span>
        </div>
        <nav aria-label="Main" className="flex flex-col gap-0.5 px-2">
          {NAV.filter((item) => !("adminOnly" in item) || isAdmin).map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={"end" in item}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-sm text-ink-2 hover:bg-surface-2 hover:text-ink",
                  isActive && "bg-accent-wash/60 font-medium text-ink",
                )
              }
            >
              <item.icon className="size-4" aria-hidden />
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto px-4 py-3 text-[11px] leading-4 text-muted">
          Provider sanctions &amp; exclusions reconciliation
        </div>
      </aside>
      <div className="flex min-w-0 flex-col">
        <TopBar />
        <main className="min-w-0 flex-1 overflow-y-auto px-6 py-5">
          <div className="mx-auto max-w-[1400px]">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}

function TopBar() {
  const user = useUser();
  const { logout } = useAuth();
  const navigate = useNavigate();
  const [theme, setTheme] = useTheme();
  const ThemeIcon = theme === "dark" ? Moon : theme === "light" ? Sun : Monitor;

  return (
    <header className="flex h-12 shrink-0 items-center justify-end gap-2 border-b bg-surface px-6">
      <MenuRoot>
        <MenuTrigger asChild>
          <Button variant="ghost" size="icon" aria-label={`Theme: ${theme}`}>
            <ThemeIcon />
          </Button>
        </MenuTrigger>
        <MenuContent>
          <MenuLabel>Theme</MenuLabel>
          <MenuItem onSelect={() => setTheme("system")}>
            <Monitor /> System
          </MenuItem>
          <MenuItem onSelect={() => setTheme("light")}>
            <Sun /> Light
          </MenuItem>
          <MenuItem onSelect={() => setTheme("dark")}>
            <Moon /> Dark
          </MenuItem>
        </MenuContent>
      </MenuRoot>
      <MenuRoot>
        <MenuTrigger asChild>
          <Button variant="ghost" className="gap-2">
            <span className="grid size-6 place-items-center rounded-full bg-accent text-[11px] font-semibold text-accent-fg" aria-hidden>
              {user.email.charAt(0).toUpperCase()}
            </span>
            <span className="max-w-48 truncate">{user.full_name || user.email}</span>
            <span className="rounded-sm bg-surface-2 px-1.5 py-px text-[11px] text-ink-2 ring-1 ring-border">{user.role}</span>
          </Button>
        </MenuTrigger>
        <MenuContent>
          <MenuLabel>{user.email}</MenuLabel>
          <MenuSeparator />
          <MenuItem
            onSelect={async () => {
              await logout();
              navigate("/login", { replace: true });
            }}
          >
            <LogOut /> Sign out
          </MenuItem>
        </MenuContent>
      </MenuRoot>
    </header>
  );
}
