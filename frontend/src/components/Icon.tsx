export function Icon({
  name,
  size = 20,
}: {
  name: "shield" | "upload" | "video" | "check" | "clock" | "arrow";
  size?: number;
}) {
  const paths = {
    shield: "M12 3 3 7v5c0 5 9 9 9 9s9-4 9-9V7l-9-4ZM8 12l3 3 5-6",
    upload: "M12 16V3m-5 5 5-5 5 5M4 15v6h16v-6",
    video: "M3 4h18v16H3zM3 8h18M7 4v4m5-4v4m5-4v4M10 12l5 3-5 3z",
    check: "m5 12 4 4L19 6",
    clock: "M12 8v5l3 2M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0",
    arrow: "M4 12h16m-6-6 6 6-6 6",
  };
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name]} />
    </svg>
  );
}
