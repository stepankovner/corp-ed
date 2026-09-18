import { Navigate, Route, Routes } from "react-router-dom";

import { Layout } from "./components/Layout";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { useAuth } from "./auth/AuthContext";
import { BriefPage } from "./pages/BriefPage";
import { ChatPage } from "./pages/ChatPage";
import { InternPage } from "./pages/InternPage";
import { LoginPage } from "./pages/LoginPage";
import { MaterialsPage } from "./pages/MaterialsPage";
import { ProgramPage } from "./pages/ProgramPage";

/** Каждой роли — свой первый экран. */
function HomeRedirect() {
  const { user } = useAuth();
  return <Navigate to={user?.role === "manager" ? "/materials" : "/my"} replace />;
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />

      <Route
        element={
          <ProtectedRoute>
            <Layout />
          </ProtectedRoute>
        }
      >
        <Route index element={<HomeRedirect />} />
        <Route path="materials" element={<MaterialsPage />} />
        <Route path="programs" element={<ProgramPage />} />
        <Route path="programs/new" element={<BriefPage />} />
        <Route path="programs/:programId" element={<ProgramPage />} />
        <Route path="chat" element={<ChatPage />} />
        <Route path="my" element={<InternPage />} />
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
