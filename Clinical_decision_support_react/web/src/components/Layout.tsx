import { ReactNode, useEffect, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { initials } from "./ui";

export const NAV: { section: string; items: { page: string; to: string; label: string; icon: string }[] }[] = [
  {
    section: "Clinical",
    items: [
      { page: "dashboard", to: "/dashboard", label: "Clinical Overview", icon: "▦" },
      { page: "consultations", to: "/consultations", label: "Consultations", icon: "✚" },
      { page: "patients", to: "/registry", label: "Patients", icon: "☺" },
      { page: "knowledge", to: "/knowledge", label: "Knowledge Base", icon: "❏" },
    ],
  },
  {
    section: "Revenue cycle",
    items: [
      { page: "coding", to: "/coding", label: "Coding & HCC", icon: "⌗" },
      { page: "billing", to: "/billing", label: "Billing & RCM", icon: "$" },
    ],
  },
  {
    section: "Administration",
    items: [
      { page: "oversight", to: "/oversight", label: "Oversight", icon: "◉" },
      { page: "users", to: "/users", label: "User Access", icon: "⚿" },
    ],
  },
];

/** PHI classes and the permission that unmasks each (server: app/domain/masking.py). */
export const PHI_CLASSES: { module: string; label: string }[] = [
  { module: "PHI: Identity", label: "name, DOB, MRN" },
  { module: "PHI: Contact details", label: "phone, email, address" },
  { module: "PHI: Clinical narrative", label: "conversation and notes" },
];

export function Layout({ children }: { children: ReactNode }) {
  const { user, logout, can } = useAuth();
  const [open, setOpen] = useState(false);
  const location = useLocation();
  useEffect(() => setOpen(false), [location.pathname]);
  if (!user) return null;
  const pages = new Set(user.pages);

  return (
    <div className="shell">
      <aside className={`sidebar ${open ? "open" : ""}`} aria-label="Main navigation">
        <div className="brand">
          <span className="brand-mark">N</span>
          <span>NuuCare<small>Agent Pod</small></span>
        </div>
        {NAV.map((group) => {
          const items = group.items.filter((item) => pages.has(item.page));
          if (!items.length) return null;
          return (
            <nav key={group.section}>
              <div className="nav-section">{group.section}</div>
              {items.map((item) => (
                <NavLink key={item.to} to={item.to} className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}>
                  <span className="nav-icon" aria-hidden>{item.icon}</span>
                  {item.label}
                </NavLink>
              ))}
            </nav>
          );
        })}
        <div className="sidebar-foot">
          <MaskedNotice masked={PHI_CLASSES.filter((c) => !can(c.module)).map((c) => c.label)} />
          <div className="user-chip">
            <span className="avatar">{initials(user.name)}</span>
            <div style={{ minWidth: 0 }}>
              <b>{user.name}</b>
              <small>{user.role}</small>
            </div>
          </div>
          <button className="btn ghost block" onClick={() => void logout()}>Sign out</button>
        </div>
      </aside>
      <div>
        <div className="mobile-bar">
          <button className="btn sm" aria-label="Open menu" onClick={() => setOpen(!open)}>☰</button>
          <b>NuuCare</b>
        </div>
        <main className="main">{children}</main>
      </div>
    </div>
  );
}

function MaskedNotice({ masked }: { masked: string[] }) {
  if (!masked.length) return null;
  return (
    <div className="masked-note" role="note" title="Masked by your role (minimum necessary). Ask an administrator if you need access.">
      <b>Masked view</b>
      <span>Hidden for your role: {masked.join("; ")}</span>
    </div>
  );
}
