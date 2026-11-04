import { useEffect, useState } from "react";
import RunsPage from "./RunsPage";
import RunPage from "./RunPage";

// Hash routes: #/ for the list, #/runs/<id> for one check.
function useRoute() {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const on = () => setHash(window.location.hash);
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const m = hash.match(/^#\/runs\/([\w-]+)/);
  return m ? { page: "run" as const, id: m[1] } : { page: "runs" as const };
}

export default function App() {
  const route = useRoute();
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [route.page]);
  return (
    <>
      <header className="top">
        <a className="wordmark" href="#/">
          Responsible Research Agent <span>citation checks</span>
        </a>
        <nav>{route.page === "run" && <a href="#/">All checks</a>}</nav>
      </header>
      {route.page === "run" ? <RunPage id={route.id} key={route.id} /> : <RunsPage />}
    </>
  );
}
