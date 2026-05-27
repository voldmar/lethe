use std::{
    env, fs,
    path::{Path, PathBuf},
};

fn main() {
    println!("cargo:rerun-if-changed=build.rs");

    #[cfg(target_os = "linux")]
    {
        if let Some(link_dir) = prepare_libstdcxx_link_dir() {
            println!("cargo:rustc-link-search=native={}", link_dir.display());
        }
    }
}

#[cfg(target_os = "linux")]
fn prepare_libstdcxx_link_dir() -> Option<PathBuf> {
    let lib = find_libstdcxx()?;
    let out_dir = PathBuf::from(env::var_os("OUT_DIR")?);
    let link = out_dir.join("libstdc++.so");

    if !link.exists() {
        let _ = std::os::unix::fs::symlink(&lib, &link)
            .or_else(|_| fs::copy(&lib, &link).map(|_| ()))
            .ok()?;
    }

    Some(out_dir)
}

#[cfg(target_os = "linux")]
fn find_libstdcxx() -> Option<PathBuf> {
    [
        "/usr/lib64/libstdc++.so.6",
        "/lib64/libstdc++.so.6",
        "/usr/lib/x86_64-linux-gnu/libstdc++.so.6",
        "/lib/x86_64-linux-gnu/libstdc++.so.6",
    ]
    .iter()
    .map(Path::new)
    .find(|path| path.exists())
    .map(Path::to_path_buf)
}
