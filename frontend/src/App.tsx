import { Navigate, Route, Routes } from "react-router-dom";

import { Layout } from "./components/Layout";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { useAuth } from "./auth/AuthContext";
import { BriefsPage } from "./pages/BriefsPage";
import { ChatPage } from "./pages/ChatPage";
import { LoginPage } from "./pages/LoginPage";
import { MaterialsPage } from "./pages/MaterialsPage";
import { ProgramPage } from "./pages/ProgramPage";

/** Каждой роли — свой первый экран. */
function HomeRedirect() {
  const { user } = useAuth();
  return <Navigate to={user?.role === "manager" ? "/materials" : "/chat"} replace />;
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
        <Route path="briefs" element={<BriefsPage />} />
        <Route path="programs/:programId" element={<ProgramPage />} />
        <Route path="chat" element={<ChatPage />} />
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
