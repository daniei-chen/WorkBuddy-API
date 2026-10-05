'use client';

import {useTheme} from 'next-themes';
import {Toaster as Sonner, ToasterProps} from 'sonner';

/**
 * 全局 Toaster。
 *
 * 说明：业务提示走 lib/toast.tsx 的 toast.custom 自定义卡片，
 * 因此这里不再通过内联样式指定背景/边框 —— 内联样式优先级高于 CSS，
 * 会给自定义卡片叠上一层底色，形成「双层边框」的观感。
 * 仅保留位置与主题，其余交给 sonner 默认值。
 */
const Toaster = ({...props}: ToasterProps) => {
  const {theme = 'system'} = useTheme();

  return (
    <Sonner
      theme={theme as ToasterProps['theme']}
      className="toaster group"
      position="top-center"
      offset={16}
      gap={10}
      visibleToasts={4}
      toastOptions={{
        classNames: {
          // 去掉默认留白，让自定义卡片贴合边缘
          toast: 'group',
        },
      }}
      {...props}
    />
  );
};

export {Toaster};
