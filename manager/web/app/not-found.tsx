'use client';
import {motion} from 'motion/react';
import {Button} from '@/components/ui/button';
import {Home} from 'lucide-react';
import Link from 'next/link';
import {isCjkLocale} from '@/lib/i18n';
import {useI18n} from '@/lib/i18n/provider';

export default function NotFound() {
  const {t, locale} = useI18n();
  // 逐字入场动画对中文/日文成立；拉丁文字按字符拆会变成 P-a-g-e，改为按词拆
  const split = (text: string) => (isCjkLocale(locale) ? text.split('') : text.split(' '));
  const lead = t('notFound.line1');
  const tail = t('notFound.line2');

  return (
    <div className="fixed inset-0 flex items-center justify-center dark:bg-black bg-white">
      <div className="mx-auto max-w-7xl px-4 text-center sm:px-6 md:px-8 lg:px-12">
        <motion.div
          initial={{opacity: 0, y: 20}}
          animate={{opacity: 1, y: 0}}
          transition={{duration: 0.6, delay: 0.2}}
        >
          <p className="font-bold text-xl md:text-4xl dark:text-white text-black">
            {split(lead).map((word, idx) => (
              <motion.span
                key={idx}
                className="inline-block"
                initial={{x: -10, opacity: 0}}
                animate={{x: 0, opacity: 1}}
                transition={{duration: 0.5, delay: idx * 0.04}}
              >
                {word}
              </motion.span>
            ))}
            <span className="text-neutral-400">
              {split(tail).map((word, idx) => (
                <motion.span
                  key={idx}
                  className="inline-block"
                  initial={{x: -10, opacity: 0}}
                  animate={{x: 0, opacity: 1}}
                  transition={{duration: 0.5, delay: (idx + 2) * 0.04}}
                >
                  {word}
                </motion.span>
              ))}
            </span>
          </p>
        </motion.div>

        <motion.div
          initial={{opacity: 0, y: 20}}
          animate={{opacity: 1, y: 0}}
          transition={{duration: 0.6, delay: 0.6}}
        >
          <p className="text-sm md:text-lg text-neutral-500 mx-auto max-w-2xl py-4">
            {t('notFound.desc')}
          </p>
        </motion.div>

        <motion.div
          initial={{opacity: 0, y: 20}}
          animate={{opacity: 1, y: 0}}
          transition={{duration: 0.6, delay: 1.0}}
          className="flex justify-center pt-6"
        >
          <Link href="/dashboard">
            <Button size="lg" className="rounded-full">
              <Home className="mr-2 h-4 w-4" />
              {t('notFound.back')}
            </Button>
          </Link>
        </motion.div>
      </div>
    </div>
  );
}
