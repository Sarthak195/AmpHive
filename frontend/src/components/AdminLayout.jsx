/**
 * AdminLayout — STUB (Phase E rebuilds this into the full console sidebar
 * shell). For now it stamps the console theme with the admin accent and
 * renders admin pages in a sidebar-less console main column.
 */

import { Outlet } from 'react-router-dom';
import useTheme from '../hooks/useTheme';

const AdminLayout = () => {
  useTheme('volt', 'admin');
  return (
    <main className="console-main">
      <Outlet />
    </main>
  );
};

export default AdminLayout;
