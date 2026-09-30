/* One source for every in-app mark and the browser favicon. The version query
   refreshes browsers that keep favicons in a cache separate from page assets. */

const markUrl = "/favicon.svg?v=47ff778c";

export function Mark({ size = 32 }: { size?: number }) {
  return (
    <img
      className="mark"
      src={markUrl}
      width={size}
      height={size}
      alt=""
      aria-hidden="true"
      draggable={false}
    />
  );
}
