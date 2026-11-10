import { Navigate, createBrowserRouter } from 'react-router-dom';
import MainLayout from '../layouts/MainLayout';
import NotFound from '../pages/NotFound';
import ComingSoon from '../pages/ComingSoon';
import DashboardPage from '../pages/dashboard';

export const router = createBrowserRouter([
  {
    path: '/',
    element: <MainLayout />,
    children: [
      { index: true, element: <Navigate to="/dashboard" replace /> },
      { path: 'dashboard', element: <DashboardPage /> },
      { path: 'contracts', element: <ComingSoon name="合同管理" /> },
      { path: 'reviews', element: <ComingSoon name="智能审核" /> },
      { path: 'approvals', element: <ComingSoon name="审批中心" /> },
      { path: 'knowledge', element: <ComingSoon name="知识库" /> },
      { path: '*', element: <NotFound /> },
    ],
  },
]);
