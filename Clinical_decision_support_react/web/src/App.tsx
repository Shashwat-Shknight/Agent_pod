import { lazy, ReactNode, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth/AuthContext";
import { BootstrapPage, ChangePasswordPage, SignInPage } from "./auth/AuthPages";
import { Layout, NAV } from "./components/Layout";
import { Banner, Loading } from "./components/ui";

const Dashboard = lazy(() => import("./pages/Dashboard"));
const Consultations = lazy(() => import("./pages/Consultations"));
const Patients = lazy(() => import("./pages/Patients"));
const NewConsultation = lazy(() => import("./pages/NewConsultation"));
const Review = lazy(() => import("./pages/Review"));
const Coding = lazy(() => import("./pages/Coding"));
const Billing = lazy(() => import("./pages/Billing"));
const Oversight = lazy(() => import("./pages/Oversight"));
const Users = lazy(() => import("./pages/Users"));
const Knowledge = lazy(() => import("./pages/Knowledge"));

/** Route guard: a page the role cannot open shows an explanation instead of a broken screen. */
function Guard({ page, children }: { page: string; children: ReactNode }) {
  const { user } = useAuth();
  if (!user?.pages.includes(page)) {
    return (
      <Banner tone="warn" title="You don't have access to this page">
        Your role ({user?.role}) cannot open it. If you need access, ask an administrator.
      </Banner>
    );
  }
  return <>{children}</>;
}

function home(pages: string[]) {
  for (const group of NAV) for (const item of group.items) if (pages.includes(item.page)) return item.to;
  return "/no-access";
}

export default function App() {
  const { state, user, offlineError, refresh } = useAuth();

  if (state === "loading") return <Loading label="Connecting to NuuCare…" />;
  if (state === "offline") {
    return (
      <main className="auth-page">
        <section className="card auth-card">
          <Banner tone="error" title="Cannot reach the NuuCare server" action={<button className="btn sm" onClick={() => void refresh()}>Retry</button>}>
            {offlineError} Start it with: .venv\Scripts\python -m uvicorn app.main:app --port 8000
          </Banner>
        </section>
      </main>
    );
  }
  if (state === "bootstrap") return <BootstrapPage />;
  if (state === "signed_out" || !user) return <SignInPage />;
  if (state === "password_change") return <ChangePasswordPage />;

  return (
    <Layout>
      <Suspense fallback={<Loading />}>
        <Routes>
          <Route path="/" element={<Navigate to={home(user.pages)} replace />} />
          <Route path="/dashboard" element={<Guard page="dashboard"><Dashboard /></Guard>} />
          <Route path="/consultations" element={<Guard page="consultations"><Consultations /></Guard>} />
          <Route path="/registry" element={<Guard page="patients"><Patients /></Guard>} />
          <Route path="/consultations/new" element={<Guard page="consultations"><NewConsultation /></Guard>} />
          <Route path="/consultations/:id" element={<Guard page="review"><Review /></Guard>} />
          <Route path="/coding" element={<Guard page="coding"><Coding /></Guard>} />
          <Route path="/coding/:id" element={<Guard page="coding"><Coding /></Guard>} />
          <Route path="/billing" element={<Guard page="billing"><Billing /></Guard>} />
          <Route path="/billing/:id" element={<Guard page="billing"><Billing /></Guard>} />
          <Route path="/oversight" element={<Guard page="oversight"><Oversight /></Guard>} />
          <Route path="/users" element={<Guard page="users"><Users /></Guard>} />
          <Route path="/knowledge" element={<Guard page="knowledge"><Knowledge /></Guard>} />
          <Route path="/no-access" element={<Banner tone="warn" title="Your role has no screens in this workspace">Ask an administrator to adjust your role.</Banner>} />
          <Route path="*" element={<Banner tone="warn" title="Page not found">Use the menu on the left.</Banner>} />
        </Routes>
      </Suspense>
    </Layout>
  );
}
