/** Message of a failed rpc(): the server payload lives in `e.data.message`. */
export function rpcErrorMessage(e, fallback) {
    return e?.data?.message || e?.message?.data?.message || fallback;
}
